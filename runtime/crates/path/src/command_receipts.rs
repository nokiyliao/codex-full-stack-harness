//! Access to a session's command receipts.
//!
//! On Unix, the session path is only used to open a trusted directory one
//! component at a time. Every subsequent operation is relative to the held
//! `command_receipts` directory descriptor. Display paths are not authority.

use std::io;
use std::path::{Path, PathBuf};

/// Process-owned receipt location. This is not a command_run payload or an
/// authority to access another session: the caller's workspace must match.
pub const RECEIPT_ROOT_ENV: &str = "TURA_COMMAND_RECEIPT_ROOT";
pub const RECEIPT_WORKSPACE_ENV: &str = "TURA_COMMAND_RECEIPT_WORKSPACE";

#[derive(Debug)]
pub struct ReceiptBinding {
    pub root: PathBuf,
    pub workspace: PathBuf,
}

impl ReceiptBinding {
    pub fn new(root: impl AsRef<Path>, workspace: impl AsRef<Path>) -> Self {
        Self { root: root.as_ref().to_path_buf(), workspace: workspace.as_ref().to_path_buf() }
    }

    fn from_process() -> io::Result<Option<Self>> {
        Self::from_parts(std::env::var_os(RECEIPT_ROOT_ENV), std::env::var_os(RECEIPT_WORKSPACE_ENV))
    }

    fn from_parts(root: Option<std::ffi::OsString>, workspace: Option<std::ffi::OsString>) -> io::Result<Option<Self>> {
        match (root, workspace) {
            (None, None) => Ok(None),
            (Some(root), Some(workspace)) => Ok(Some(Self::new(PathBuf::from(root), PathBuf::from(workspace)))),
            _ => Err(io::Error::new(io::ErrorKind::InvalidInput, "partial command receipt binding")),
        }
    }
}

fn checked_binding<'a>(session: &Path, binding: &'a ReceiptBinding) -> io::Result<&'a Path> {
    use std::path::Component;
    let normal_absolute = |path: &Path| {
        path.is_absolute()
            && path.components().map(|part| part.as_os_str()).collect::<PathBuf>().as_os_str()
                == path.as_os_str()
            && path.components().all(|part| matches!(
            part, Component::RootDir | Component::Prefix(_) | Component::Normal(_)
        )) && path.components().any(|part| matches!(part, Component::Normal(_)))
    };
    if !normal_absolute(session) || !normal_absolute(&binding.root) || !normal_absolute(&binding.workspace)
        || session != binding.workspace || binding.root.starts_with(&binding.workspace)
    {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid command receipt binding or workspace"));
    }
    Ok(&binding.root)
}

const MAX_NAME_BYTES: usize = 255;

fn validate_name(name: &str) -> io::Result<()> {
    if name.is_empty()
        || name.len() > MAX_NAME_BYTES
        || matches!(name, "." | "..")
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "receipt name must be one bounded ASCII filename component",
        ));
    }
    Ok(())
}

#[cfg(unix)]
mod unix {
    use super::{Path, PathBuf, ReceiptBinding, checked_binding, validate_name};
    use rustix::fs::{
        AtFlags, CWD, Dir, Mode, OFlags, fsync, linkat, mkdirat, openat, renameat, unlinkat,
    };
    use rustix::io::Errno;
    use std::fs::File;
    use std::io::{self, Read, Write};
    use std::path::Component;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    const MAX_RECEIPT_BYTES: u64 = 16 * 1024 * 1024;
    const MAX_TEMP_ATTEMPTS: usize = 16;
    static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    #[derive(Debug)]
    pub struct ReceiptStore {
        directory: File,
        display_directory: PathBuf,
    }

    impl ReceiptStore {
        /// Open a trusted absolute session directory without following any
        /// ancestor symlink. Missing receipt directories are created privately.
        pub fn open(session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Self::open_bound(session_dir, ReceiptBinding::from_process()?.as_ref())
        }

        /// Open an existing receipt directory without materializing state.
        /// `NotFound` means only that the receipt subtree is absent; a missing
        /// session root is reported as `InvalidInput` instead.
        pub fn open_existing(session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Self::open_existing_bound(session_dir, ReceiptBinding::from_process()?.as_ref())
        }

        /// Pure binding path for tests and non-process callers. No env mutation.
        pub fn open_bound(session_dir: impl AsRef<Path>, binding: Option<&ReceiptBinding>) -> io::Result<Self> {
            Self::open_inner(session_dir.as_ref(), binding, true)
        }

        pub fn open_existing_bound(session_dir: impl AsRef<Path>, binding: Option<&ReceiptBinding>) -> io::Result<Self> {
            Self::open_inner(session_dir.as_ref(), binding, false)
        }

        fn open_inner(session_dir: &Path, binding: Option<&ReceiptBinding>, create: bool) -> io::Result<Self> {
            // Validate all binding fields and both existing roots before any mkdir.
            let (mut directory, children, display_directory) = if let Some(binding) = binding {
                let root = checked_binding(session_dir, binding)?;
                let _workspace = open_session_dir(session_dir)?;
                (open_session_dir(root)?, & ["command_receipts"][..], root.join("command_receipts"))
            } else {
                (open_session_dir(session_dir)?, & [".tura", "run", "command_receipts"][..],
                 session_dir.join(".tura/run/command_receipts"))
            };
            for name in children {
                directory = open_child_dir(&directory, name, create)?;
            }
            Ok(Self {
                directory,
                display_directory,
            })
        }

        /// Return the conventional path for diagnostics only. Never use this
        /// value as a read or write authority.
        pub fn display_path(&self, name: &str) -> io::Result<PathBuf> {
            validate_name(name)?;
            Ok(self.display_directory.join(name))
        }

        pub fn read_optional(&self, name: &str) -> io::Result<Option<Vec<u8>>> {
            let Some(mut file) = self.open_regular(name)? else {
                return Ok(None);
            };
            if file.metadata()?.len() > MAX_RECEIPT_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt is too large",
                ));
            }
            let mut bytes = Vec::new();
            (&mut file)
                .take(MAX_RECEIPT_BYTES + 1)
                .read_to_end(&mut bytes)?;
            if bytes.len() as u64 > MAX_RECEIPT_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt is too large",
                ));
            }
            Ok(Some(bytes))
        }

        pub fn read(&self, name: &str) -> io::Result<Vec<u8>> {
            self.read_optional(name)?
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        }

        /// Publish complete bytes without replacing any existing entry.
        pub fn publish_new(&self, name: &str, bytes: &[u8]) -> io::Result<()> {
            validate_name(name)?;
            validate_bytes(bytes)?;
            let mut temp = TempEntry::create(&self.directory)?;
            temp.write_and_sync(bytes)?;
            linkat(
                &self.directory,
                &temp.name,
                &self.directory,
                name,
                AtFlags::empty(),
            )?;
            temp.remove()?;
            fsync(&self.directory)?;
            Ok(())
        }

        /// Replace a currently regular-file entry with complete caller-
        /// validated bytes. This is atomic publication, not a content CAS;
        /// callers must separately enforce their single-writer ownership.
        pub fn replace(&self, name: &str, bytes: &[u8]) -> io::Result<()> {
            validate_bytes(bytes)?;
            let _existing = self
                .open_regular(name)?
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
            let mut temp = TempEntry::create(&self.directory)?;
            temp.write_and_sync(bytes)?;
            renameat(&self.directory, &temp.name, &self.directory, name)?;
            temp.disarm();
            fsync(&self.directory)?;
            Ok(())
        }

        /// List only valid, regular-file receipt names. A non-regular entry
        /// with a valid public name is an error, not an accepted receipt.
        pub fn list_names(&self) -> io::Result<Vec<String>> {
            let mut entries = Dir::read_from(&self.directory)?;
            let mut names = Vec::new();
            while let Some(entry) = entries.read() {
                let entry = entry?;
                let Ok(name) = entry.file_name().to_str() else {
                    continue;
                };
                if validate_name(name).is_err() {
                    continue;
                }
                match self.open_regular(name)? {
                    Some(_) => names.push(name.to_owned()),
                    None => continue, // Removed while enumerating.
                }
            }
            names.sort_unstable();
            Ok(names)
        }

        fn open_regular(&self, name: &str) -> io::Result<Option<File>> {
            validate_name(name)?;
            let flags = OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK;
            let file: File = match openat(&self.directory, name, flags, Mode::empty()) {
                Ok(file) => file.into(),
                Err(Errno::NOENT) => return Ok(None),
                Err(error) => return Err(error.into()),
            };
            if !file.metadata()?.file_type().is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt entry is not a regular file",
                ));
            }
            Ok(Some(file))
        }
    }

    fn validate_bytes(bytes: &[u8]) -> io::Result<()> {
        if bytes.len() as u64 > MAX_RECEIPT_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "receipt exceeds maximum size",
            ));
        }
        Ok(())
    }

    fn open_session_dir(session_dir: &Path) -> io::Result<File> {
        if !session_dir.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "session directory must be absolute",
            ));
        }
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let mut directory: File = openat(CWD, Path::new("/"), flags, Mode::empty())?.into();
        for component in session_dir.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(name) => {
                    directory = openat(&directory, name, flags, Mode::empty())
                        .map_err(|error| {
                            if error == Errno::NOENT {
                                io::Error::new(
                                    io::ErrorKind::InvalidInput,
                                    "session directory does not exist",
                                )
                            } else {
                                error.into()
                            }
                        })?
                        .into();
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "session directory contains a non-normal component",
                    ));
                }
            }
        }
        Ok(directory)
    }

    fn open_child_dir(parent: &File, name: &str, create: bool) -> io::Result<File> {
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        match openat(parent, name, flags, Mode::empty()) {
            Ok(directory) => Ok(directory.into()),
            Err(Errno::NOENT) if create => {
                match mkdirat(parent, name, Mode::RWXU) {
                    Ok(()) => fsync(parent)?,
                    Err(Errno::EXIST) => {}
                    Err(error) => return Err(error.into()),
                }
                Ok(openat(parent, name, flags, Mode::empty())?.into())
            }
            Err(error) => Err(error.into()),
        }
    }

    struct TempEntry<'a> {
        directory: &'a File,
        name: String,
        file: File,
        active: bool,
    }

    impl<'a> TempEntry<'a> {
        fn create(directory: &'a File) -> io::Result<Self> {
            let flags =
                OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW;
            for _ in 0..MAX_TEMP_ATTEMPTS {
                let nanos = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos();
                let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
                let name = format!(
                    "~command-receipt-tmp-{:x}-{nanos:x}-{sequence:x}",
                    std::process::id()
                );
                match openat(directory, name.as_str(), flags, Mode::RUSR | Mode::WUSR) {
                    Ok(file) => {
                        return Ok(Self {
                            directory,
                            name,
                            file: file.into(),
                            active: true,
                        });
                    }
                    Err(Errno::EXIST) => continue,
                    Err(error) => return Err(error.into()),
                }
            }
            Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "could not reserve a receipt temp entry",
            ))
        }

        fn write_and_sync(&mut self, bytes: &[u8]) -> io::Result<()> {
            self.file.write_all(bytes)?;
            self.file.sync_all()
        }

        fn remove(&mut self) -> io::Result<()> {
            unlinkat(self.directory, self.name.as_str(), AtFlags::empty())?;
            self.active = false;
            Ok(())
        }

        fn disarm(&mut self) {
            self.active = false;
        }
    }

    impl Drop for TempEntry<'_> {
        fn drop(&mut self) {
            if self.active {
                let _ = unlinkat(self.directory, self.name.as_str(), AtFlags::empty());
            }
        }
    }
}

#[cfg(unix)]
pub use unix::ReceiptStore;

#[cfg(windows)]
mod windows {
    use super::{Path, PathBuf, ReceiptBinding, checked_binding, validate_name as validate_basic_name};
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Read, Write};
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Component;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    const MAX_RECEIPT_BYTES: u64 = 16 * 1024 * 1024;
    const MAX_TEMP_ATTEMPTS: usize = 16;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn validate_name(name: &str) -> io::Result<()> {
        validate_basic_name(name)?;
        let stem = name
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && matches!(stem.as_bytes()[3], b'1'..=b'9'));
        if reserved || name.ends_with('.') {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "receipt name is reserved on Windows",
            ));
        }
        Ok(())
    }

    // Windows std has no handle-relative rename; recheck the plain directory
    // before each path-based operation rather than claiming Unix FD anchoring.
    #[derive(Debug)]
    pub struct ReceiptStore {
        directory: PathBuf,
        display_directory: PathBuf,
        canonical_directory: PathBuf,
        creation_time: u64,
    }

    impl ReceiptStore {
        pub fn open(session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Self::open_bound(session_dir, ReceiptBinding::from_process()?.as_ref())
        }

        pub fn open_existing(session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Self::open_existing_bound(session_dir, ReceiptBinding::from_process()?.as_ref())
        }

        pub fn open_bound(session_dir: impl AsRef<Path>, binding: Option<&ReceiptBinding>) -> io::Result<Self> {
            Self::open_inner(session_dir.as_ref(), binding, true)
        }

        pub fn open_existing_bound(session_dir: impl AsRef<Path>, binding: Option<&ReceiptBinding>) -> io::Result<Self> {
            Self::open_inner(session_dir.as_ref(), binding, false)
        }

        fn open_inner(session_dir: &Path, binding: Option<&ReceiptBinding>, create: bool) -> io::Result<Self> {
            let bound_root = binding.map(|binding| checked_binding(session_dir, binding)).transpose()?;
            let session = checked_directory(session_dir).map_err(|error| {
                if error.kind() == io::ErrorKind::NotFound {
                    io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "session directory does not exist",
                    )
                } else {
                    error
                }
            })?;
            let (mut directory, children) = if let Some(root) = bound_root {
                (checked_directory(root)?, & ["command_receipts"][..])
            } else {
                (session, & [".tura", "run", "command_receipts"][..])
            };
            let display_directory = directory.join(children.join("/"));
            for name in children {
                directory.push(name);
                if create {
                    match fs::create_dir(&directory) {
                        Ok(()) => {}
                        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                        Err(error) => return Err(error),
                    }
                }
                checked_directory(&directory)?;
            }
            let canonical_directory = fs::canonicalize(&directory)?;
            let creation_time = fs::metadata(&directory)?.creation_time();
            Ok(Self {
                directory,
                display_directory,
                canonical_directory,
                creation_time,
            })
        }

        pub fn display_path(&self, name: &str) -> io::Result<PathBuf> {
            validate_name(name)?;
            Ok(self.display_directory.join(name))
        }

        pub fn read_optional(&self, name: &str) -> io::Result<Option<Vec<u8>>> {
            let Some(mut file) = self.open_regular(name)? else {
                return Ok(None);
            };
            if file.metadata()?.len() > MAX_RECEIPT_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt is too large",
                ));
            }
            let mut bytes = Vec::new();
            (&mut file)
                .take(MAX_RECEIPT_BYTES + 1)
                .read_to_end(&mut bytes)?;
            if bytes.len() as u64 > MAX_RECEIPT_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt is too large",
                ));
            }
            Ok(Some(bytes))
        }

        pub fn read(&self, name: &str) -> io::Result<Vec<u8>> {
            self.read_optional(name)?
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        }

        pub fn publish_new(&self, name: &str, bytes: &[u8]) -> io::Result<()> {
            validate_name(name)?;
            validate_bytes(bytes)?;
            self.check_binding()?;
            let mut temp = TempEntry::create(&self.directory)?;
            temp.write_and_sync(bytes)?;
            self.check_binding()?;
            fs::hard_link(&temp.path, self.directory.join(name))?;
            temp.remove()
        }

        pub fn replace(&self, name: &str, bytes: &[u8]) -> io::Result<()> {
            validate_bytes(bytes)?;
            let existing = self
                .open_regular(name)?
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
            drop(existing);
            let mut temp = TempEntry::create(&self.directory)?;
            temp.write_and_sync(bytes)?;
            self.check_binding()?;
            fs::rename(&temp.path, self.directory.join(name))?;
            temp.disarm();
            Ok(())
        }

        pub fn list_names(&self) -> io::Result<Vec<String>> {
            self.check_binding()?;
            let mut names = Vec::new();
            for entry in fs::read_dir(&self.directory)? {
                let entry = entry?;
                let file_name = entry.file_name();
                let Some(name) = file_name.to_str() else {
                    continue;
                };
                if validate_name(name).is_err() {
                    continue;
                }
                if self.open_regular(name)?.is_some() {
                    names.push(name.to_owned());
                }
            }
            names.sort_unstable();
            Ok(names)
        }

        fn open_regular(&self, name: &str) -> io::Result<Option<File>> {
            validate_name(name)?;
            self.check_binding()?;
            let file = match OpenOptions::new()
                .read(true)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
                .open(self.directory.join(name))
            {
                Ok(file) => file,
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
                Err(error) => return Err(error),
            };
            let metadata = file.metadata()?;
            if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "receipt entry is not a regular file",
                ));
            }
            Ok(Some(file))
        }

        fn check_binding(&self) -> io::Result<()> {
            checked_directory(&self.directory)?;
            let canonical = fs::canonicalize(&self.directory)?;
            let created = fs::metadata(&self.directory)?.creation_time();
            if canonical != self.canonical_directory || created != self.creation_time {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "command receipt directory changed after binding",
                ));
            }
            Ok(())
        }
    }

    fn validate_bytes(bytes: &[u8]) -> io::Result<()> {
        if bytes.len() as u64 > MAX_RECEIPT_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "receipt exceeds maximum size",
            ));
        }
        Ok(())
    }

    fn checked_directory(path: &Path) -> io::Result<PathBuf> {
        if !path.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "session directory must be absolute",
            ));
        }
        let mut current = PathBuf::new();
        for component in path.components() {
            match component {
                Component::Prefix(_) | Component::RootDir => current.push(component.as_os_str()),
                Component::Normal(_) => {
                    current.push(component.as_os_str());
                    let metadata = fs::symlink_metadata(&current)?;
                    if !metadata.is_dir()
                        || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
                    {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "receipt directory ancestor is not a plain directory",
                        ));
                    }
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "session directory contains a non-normal component",
                    ));
                }
            }
        }
        Ok(current)
    }

    struct TempEntry {
        path: PathBuf,
        file: Option<File>,
        active: bool,
    }

    impl TempEntry {
        fn create(directory: &Path) -> io::Result<Self> {
            for _ in 0..MAX_TEMP_ATTEMPTS {
                let nanos = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos();
                let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
                let name = format!(
                    "~command-receipt-tmp-{:x}-{nanos:x}-{sequence:x}",
                    std::process::id()
                );
                let path = directory.join(name);
                match OpenOptions::new().write(true).create_new(true).open(&path) {
                    Ok(file) => {
                        return Ok(Self {
                            path,
                            file: Some(file),
                            active: true,
                        });
                    }
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(error) => return Err(error),
                }
            }
            Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "could not reserve a receipt temp entry",
            ))
        }

        fn write_and_sync(&mut self, bytes: &[u8]) -> io::Result<()> {
            let mut file = self
                .file
                .take()
                .ok_or_else(|| io::Error::other("receipt temp file is closed"))?;
            file.write_all(bytes)?;
            file.sync_all()
        }

        fn remove(&mut self) -> io::Result<()> {
            fs::remove_file(&self.path)?;
            self.active = false;
            Ok(())
        }

        fn disarm(&mut self) {
            self.active = false;
        }
    }

    impl Drop for TempEntry {
        fn drop(&mut self) {
            if self.active {
                let _ = fs::remove_file(&self.path);
            }
        }
    }
}

#[cfg(windows)]
pub use windows::ReceiptStore;

#[cfg(not(any(unix, windows)))]
mod unsupported {
    use super::{Path, PathBuf};
    use std::io;

    #[derive(Debug)]
    pub struct ReceiptStore;

    impl ReceiptStore {
        fn unsupported() -> io::Error {
            io::Error::from(io::ErrorKind::Unsupported)
        }

        pub fn open(_session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Err(Self::unsupported())
        }
        pub fn open_existing(_session_dir: impl AsRef<Path>) -> io::Result<Self> {
            Err(Self::unsupported())
        }
        pub fn display_path(&self, _name: &str) -> io::Result<PathBuf> {
            Err(Self::unsupported())
        }
        pub fn read_optional(&self, _name: &str) -> io::Result<Option<Vec<u8>>> {
            Err(Self::unsupported())
        }
        pub fn read(&self, _name: &str) -> io::Result<Vec<u8>> {
            Err(Self::unsupported())
        }
        pub fn publish_new(&self, _name: &str, _bytes: &[u8]) -> io::Result<()> {
            Err(Self::unsupported())
        }
        pub fn replace(&self, _name: &str, _bytes: &[u8]) -> io::Result<()> {
            Err(Self::unsupported())
        }
        pub fn list_names(&self) -> io::Result<Vec<String>> {
            Err(Self::unsupported())
        }
    }
}

#[cfg(not(any(unix, windows)))]
pub use unsupported::ReceiptStore;

#[cfg(all(test, unix))]
mod tests {
    use super::{ReceiptBinding, ReceiptStore};
    use std::fs;
    use std::io::ErrorKind;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::sync::Arc;

    fn session() -> (tempfile::TempDir, PathBuf) {
        let temp = tempfile::tempdir().expect("tempdir");
        // macOS commonly exposes the temp root through the /var symlink.
        let path = fs::canonicalize(temp.path()).expect("physical temp path");
        (temp, path)
    }

    #[test]
    fn bound_state_ignores_workspace_tura_symlink_and_reopens_only_for_its_workspace() {
        let (_temp, base) = session();
        let workspace = base.join("workspace");
        let other = base.join("other");
        let state = base.join("request-state");
        let second = base.join("second-state");
        for path in [&workspace, &other, &state, &second] {
            fs::create_dir(path).expect("directory");
        }
        symlink(&other, workspace.join(".tura")).expect("preexisting workspace layout");
        let binding = ReceiptBinding::new(&state, &workspace);
        let store = ReceiptStore::open_bound(&workspace, Some(&binding)).expect("bound store");
        store.publish_new("receipt.json", b"bound").expect("publish");
        assert_eq!(store.display_path("receipt.json").unwrap(), state.join("command_receipts/receipt.json"));
        assert_eq!(ReceiptStore::open_existing_bound(&workspace, Some(&binding)).unwrap().read("receipt.json").unwrap(), b"bound");
        assert!(workspace.join(".tura").is_symlink());
        assert!(!other.join("run").exists());
        assert!(ReceiptStore::open_bound(&other, Some(&binding)).is_err());
        let isolated = ReceiptBinding::new(&second, &workspace);
        assert_eq!(ReceiptStore::open_existing_bound(&workspace, Some(&isolated)).unwrap_err().kind(), ErrorKind::NotFound);
        assert_eq!(ReceiptStore::open_bound(&workspace, Some(&isolated)).unwrap().read_optional("receipt.json").unwrap(), None);
        assert!(ReceiptStore::open_bound(&workspace, None).is_err(), "legacy symlink refusal");
    }

    #[test]
    fn bound_roots_and_children_refuse_symlinks_files_and_malformed_paths_without_writes() {
        let (_temp, base) = session();
        let workspace = base.join("workspace");
        let state = base.join("state");
        fs::create_dir(&workspace).unwrap();
        fs::create_dir(&state).unwrap();
        fs::create_dir(state.join("x")).unwrap();
        let link = base.join("link");
        symlink(&state, &link).unwrap();
        let file = base.join("file");
        fs::write(&file, b"not a directory").unwrap();
        for root in [&link, &file, &workspace, &base.join("state/../state"), &base.join("state/./x"), &base.join("missing")] {
            let binding = ReceiptBinding::new(root, &workspace);
            assert!(ReceiptStore::open_bound(&workspace, Some(&binding)).is_err(), "root {root:?}");
        }
        for wrong in [&base, &base.join("workspace/.."), &base.join("workspace/.")] {
            let binding = ReceiptBinding::new(&state, wrong);
            assert!(ReceiptStore::open_bound(&workspace, Some(&binding)).is_err());
        }
        assert!(!state.join("command_receipts").exists());
        let binding = ReceiptBinding::new(&state, &workspace);
        symlink(&workspace, state.join("command_receipts")).unwrap();
        assert!(ReceiptStore::open_bound(&workspace, Some(&binding)).is_err());
        assert!(ReceiptStore::open_existing_bound(&workspace, Some(&binding)).is_err());
        fs::remove_file(state.join("command_receipts")).unwrap();
        fs::write(state.join("command_receipts"), b"file").unwrap();
        assert!(ReceiptStore::open_bound(&workspace, Some(&binding)).is_err());
        assert!(ReceiptStore::open_existing_bound(&workspace, Some(&binding)).is_err());
        assert!(!workspace.join(".tura").exists());
    }

    #[test]
    fn partial_and_empty_bindings_fail_without_process_env_mutation() {
        assert!(ReceiptBinding::from_parts(None, None).unwrap().is_none());
        assert!(ReceiptBinding::from_parts(Some("/state".into()), None).is_err());
        assert!(ReceiptBinding::from_parts(None, Some("/workspace".into())).is_err());
        let (_temp, base) = session();
        let workspace = base.join("workspace");
        let state = base.join("state");
        fs::create_dir(&workspace).unwrap();
        fs::create_dir(&state).unwrap();
        for binding in [
            ReceiptBinding::new("", &workspace),
            ReceiptBinding::new(&state, ""),
            ReceiptBinding::new("relative", &workspace),
            ReceiptBinding::new(&state, "relative"),
            ReceiptBinding::new(&state, base.join("workspace/./")),
        ] {
            assert!(ReceiptStore::open_bound(&workspace, Some(&binding)).is_err());
        }
        assert!(!state.join("command_receipts").exists());
        assert!(!workspace.join(".tura").exists());
    }

    #[test]
    fn bound_store_remains_fd_anchored_after_child_retargeting() {
        let (_temp, base) = session();
        let workspace = base.join("workspace");
        let state = base.join("state");
        let outside = base.join("outside");
        for dir in [&workspace, &state, &outside] {
            fs::create_dir(dir).unwrap();
        }
        let binding = ReceiptBinding::new(&state, &workspace);
        let store = ReceiptStore::open_bound(&workspace, Some(&binding)).unwrap();
        let child = state.join("command_receipts");
        let moved = state.join("moved");
        fs::rename(&child, &moved).unwrap();
        symlink(&outside, &child).unwrap();
        store.publish_new("held.json", b"held").unwrap();
        assert_eq!(store.read("held.json").unwrap(), b"held");
        assert_eq!(fs::read(moved.join("held.json")).unwrap(), b"held");
        assert!(!outside.join("held.json").exists());
        assert!(ReceiptStore::open_existing_bound(&workspace, Some(&binding)).is_err());
    }

    #[test]
    fn create_publish_replace_and_list_are_fd_anchored() {
        let (_temp, session) = session();
        let store = ReceiptStore::open(&session).expect("open store");
        assert_eq!(store.read_optional("one.json").expect("read missing"), None);
        assert_eq!(
            store
                .publish_new("large.json", &vec![0; 16 * 1024 * 1024 + 1])
                .expect_err("bounded receipt")
                .kind(),
            ErrorKind::InvalidInput
        );
        store.publish_new("one.json", b"first").expect("publish");
        assert_eq!(store.read("one.json").expect("read"), b"first");
        let conflict = store
            .publish_new("one.json", b"second")
            .expect_err("no replace");
        assert_eq!(conflict.kind(), ErrorKind::AlreadyExists);
        assert_eq!(store.read("one.json").expect("read original"), b"first");
        store.replace("one.json", b"updated").expect("replace");
        assert_eq!(
            store.read("one.json").expect("read replacement"),
            b"updated"
        );
        assert_eq!(store.list_names().expect("list"), vec!["one.json"]);
        assert_eq!(
            store.display_path("one.json").expect("display"),
            session.join(".tura/run/command_receipts/one.json")
        );
    }

    #[test]
    fn open_existing_never_creates_and_ancestor_symlinks_are_rejected() {
        let (_temp, session) = session();
        assert_eq!(
            ReceiptStore::open_existing(&session)
                .expect_err("absent")
                .kind(),
            ErrorKind::NotFound
        );
        assert!(!session.join(".tura").exists());
        assert!(ReceiptStore::open("relative/session").is_err());
        assert_eq!(
            ReceiptStore::open_existing(session.join("missing-root"))
                .expect_err("missing session root")
                .kind(),
            ErrorKind::InvalidInput
        );

        let real = session.join("real");
        fs::create_dir(&real).expect("real session");
        let alias = session.join("alias");
        symlink(&real, &alias).expect("session symlink");
        assert!(ReceiptStore::open(&alias).is_err());

        symlink(&real, real.join(".tura")).expect("runtime symlink");
        assert!(ReceiptStore::open(&real).is_err());
    }

    #[test]
    fn a_retargeted_display_path_never_redirects_store_io() {
        let (_temp, session) = session();
        let outside = session.join("outside");
        fs::create_dir(&outside).expect("outside");
        let store = ReceiptStore::open(&session).expect("open store");
        let run = session.join(".tura/run");
        let original = run.join("command_receipts");
        let moved = run.join("moved_receipts");
        fs::rename(&original, &moved).expect("move directory after open");
        symlink(&outside, &original).expect("retarget display path");

        store
            .publish_new("bound.json", b"anchored")
            .expect("publish by fd");
        assert_eq!(store.read("bound.json").expect("read by fd"), b"anchored");
        assert_eq!(
            fs::read(moved.join("bound.json")).expect("moved directory"),
            b"anchored"
        );
        assert!(!outside.join("bound.json").exists());
    }

    #[test]
    fn concurrent_publishers_cannot_replace_each_other() {
        let (_temp, session) = session();
        let store = Arc::new(ReceiptStore::open(&session).expect("open store"));
        let first = Arc::clone(&store);
        let second = Arc::clone(&store);
        let a = std::thread::spawn(move || first.publish_new("race.json", b"first"));
        let b = std::thread::spawn(move || second.publish_new("race.json", b"second"));
        let results = [
            a.join().expect("first thread"),
            b.join().expect("second thread"),
        ];
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter_map(|result| result.as_ref().err())
                .next()
                .expect("one conflict")
                .kind(),
            ErrorKind::AlreadyExists
        );
        let winner = store.read("race.json").expect("winning receipt");
        assert!(winner == b"first" || winner == b"second");
    }

    #[test]
    fn invalid_names_and_nonregular_entries_fail_closed() {
        let (_temp, session) = session();
        let store = ReceiptStore::open(&session).expect("open store");
        for name in ["", ".", "..", "../escape", "sub/file", "bad name", "a\0b"] {
            assert_eq!(
                store.display_path(name).expect_err("invalid name").kind(),
                ErrorKind::InvalidInput
            );
            assert!(store.publish_new(name, b"x").is_err());
            assert!(store.read_optional(name).is_err());
        }
        assert!(store.display_path(&"a".repeat(256)).is_err());

        let directory = session.join(".tura/run/command_receipts");
        fs::write(directory.join("~command-receipt-tmp-stale"), b"hidden").expect("temp-like file");
        assert!(store.list_names().expect("private temp ignored").is_empty());
        store
            .publish_new(".hidden.json", b"valid")
            .expect("dot-prefixed call identity");
        assert_eq!(
            store.list_names().expect("list public dot name"),
            vec![".hidden.json"]
        );

        let outside = session.join("outside.json");
        fs::write(&outside, b"outside").expect("outside file");
        symlink(&outside, directory.join("link.json")).expect("receipt symlink");
        assert!(store.read_optional("link.json").is_err());
        assert!(store.replace("link.json", b"replacement").is_err());
        assert!(store.list_names().is_err());
        assert_eq!(fs::read(&outside).expect("outside unchanged"), b"outside");
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::ReceiptStore;
    use std::fs;
    use std::io::ErrorKind;
    use std::sync::Arc;

    #[test]
    fn windows_receipts_publish_without_clobber_and_replace_complete_bytes() {
        let temp = tempfile::tempdir().expect("session");
        let session = temp.path().canonicalize().expect("absolute session");
        assert_eq!(
            ReceiptStore::open_existing(&session)
                .expect_err("absent receipt store")
                .kind(),
            ErrorKind::NotFound
        );
        assert!(!session.join(".tura").exists());

        let store = Arc::new(ReceiptStore::open(&session).expect("receipt store"));
        assert_eq!(
            store.read_optional("one.json").expect("missing receipt"),
            None
        );
        let first = Arc::clone(&store);
        let second = Arc::clone(&store);
        let a = std::thread::spawn(move || first.publish_new("one.json", b"first"));
        let b = std::thread::spawn(move || second.publish_new("one.json", b"second"));
        let results = [
            a.join().expect("first writer"),
            b.join().expect("second writer"),
        ];
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter_map(|result| result.as_ref().err())
                .next()
                .expect("conflicting writer")
                .kind(),
            ErrorKind::AlreadyExists
        );
        let winner = store.read("one.json").expect("published receipt");
        assert!(winner == b"first" || winner == b"second");
        store
            .replace("one.json", b"updated")
            .expect("replace receipt");
        assert_eq!(store.read("one.json").expect("replacement"), b"updated");
        assert_eq!(store.list_names().expect("public names"), vec!["one.json"]);
        assert_eq!(
            store.display_path("one.json").expect("display path"),
            session.join(".tura/run/command_receipts/one.json")
        );
    }

    #[test]
    fn windows_receipts_reject_invalid_names_nonfiles_and_oversized_bytes() {
        let temp = tempfile::tempdir().expect("session");
        let session = temp.path().canonicalize().expect("absolute session");
        let store = ReceiptStore::open(&session).expect("receipt store");
        for name in [
            "../escape",
            "bad name",
            "NUL.json",
            "CON.json",
            "COM1.json",
            "file.",
        ] {
            assert_eq!(
                store.display_path(name).expect_err("invalid name").kind(),
                ErrorKind::InvalidInput
            );
            assert!(store.publish_new(name, b"x").is_err());
        }
        assert_eq!(
            store
                .publish_new("large.json", &vec![0; 16 * 1024 * 1024 + 1])
                .expect_err("bounded receipt")
                .kind(),
            ErrorKind::InvalidInput
        );
        let receipt_dir = session.join(".tura/run/command_receipts");
        fs::create_dir(receipt_dir.join("directory.json")).expect("non-file entry");
        assert!(store.read_optional("directory.json").is_err());
        assert!(store.replace("directory.json", b"replacement").is_err());
        assert!(store.list_names().is_err());
    }

    #[test]
    fn windows_missing_session_does_not_create_a_receipt_store() {
        let temp = tempfile::tempdir().expect("session");
        let session = temp.path().canonicalize().expect("absolute session");
        let missing = session.join("missing-session");
        assert_eq!(
            ReceiptStore::open(&missing)
                .expect_err("missing session")
                .kind(),
            ErrorKind::InvalidInput
        );
        assert!(!missing.exists());
    }
}
