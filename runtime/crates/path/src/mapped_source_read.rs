//! Contract-derived, read-only descriptors for one exact DCF logical pointer.
//!
//! This primitive is not a J-Space grant. Its callers must still authorize the
//! exact logical request. Device/inode pins identify filesystem objects, not a
//! verified volume UUID. Revalidation detects observed binding changes; it does
//! not acquire a writer lease or promise continued namespace stability.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt::{Display, Formatter};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

const BINDING_SCHEMA: &str = "dcf_mapped_source_read_binding_v1";
const CONTRACT_PATH: &str = "config/contracts/dcf_v2_contract.json";
const MAX_CONTRACT_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileSystemIdentity {
    pub device: u64,
    pub inode: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MappedSourceReadBinding {
    pub schema_version: String,
    pub contract_path: String,
    pub contract_sha256: String,
    pub contract_identity: FileSystemIdentity,
    pub repo_identity: FileSystemIdentity,
    pub requested_logical_path: String,
    pub logical_path: String,
    pub logical_namespace: String,
    pub alias_path: String,
    pub alias_link_text: String,
    pub alias_identity: FileSystemIdentity,
    pub alias_parent_identity: FileSystemIdentity,
    pub physical_root: PathBuf,
    pub physical_root_identity: FileSystemIdentity,
    pub physical_path: PathBuf,
}

#[derive(Debug)]
pub struct MappedSourceReadError {
    code: &'static str,
    detail: String,
}

impl MappedSourceReadError {
    pub fn code(&self) -> &'static str {
        self.code
    }
}

impl Display for MappedSourceReadError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for MappedSourceReadError {}

fn error(code: &'static str, detail: impl Into<String>) -> MappedSourceReadError {
    MappedSourceReadError { code, detail: detail.into() }
}

pub struct MappedSourceReadGuard {
    binding: MappedSourceReadBinding,
    file: File,
}

impl MappedSourceReadGuard {
    pub fn file(&self) -> &File {
        &self.file
    }

    pub fn binding(&self) -> &MappedSourceReadBinding {
        &self.binding
    }

    pub fn requested_logical_path(&self) -> &str {
        &self.binding.requested_logical_path
    }

    /// Call after reading, before returning any output from the descriptor.
    pub fn revalidate(&self, repo_root: &Path) -> Result<(), MappedSourceReadError> {
        self.binding.validate(repo_root)?;
        #[cfg(unix)]
        {
            let current = open_bound_target(&self.binding)?;
            if file_identity(&current)? != file_identity(&self.file)? {
                return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "exact pointer descriptor was replaced"));
            }
            self.binding.validate(repo_root)?;
        }
        Ok(())
    }
}

impl MappedSourceReadBinding {
    /// Derive only from the fixed on-disk contract and an exact logical request.
    /// Supplied bytes and their pin must match the no-follow on-disk read.
    pub fn derive(
        repo_root: &Path,
        contract_bytes: &[u8],
        expected_contract_sha256: &str,
        requested_logical_path: &str,
    ) -> Result<Self, MappedSourceReadError> {
        if contract_bytes.len() > MAX_CONTRACT_BYTES {
            return Err(error("MAPPED_SOURCE_READ_CONTRACT_TOO_LARGE", "contract byte limit"));
        }
        if expected_contract_sha256.len() != 64
            || !expected_contract_sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
            || !digest(contract_bytes).eq_ignore_ascii_case(expected_contract_sha256)
        {
            return Err(error("MAPPED_SOURCE_READ_CONTRACT_SHA_MISMATCH", "supplied contract pin"));
        }
        #[cfg(unix)]
        {
            let repo = open_absolute_directory(repo_root)?;
            let (observed, contract_identity) = read_contract(&repo)?;
            if observed != contract_bytes {
                return Err(error("MAPPED_SOURCE_READ_CONTRACT_SHA_MISMATCH", "on-disk contract bytes"));
            }
            derive_observed(repo_root, &repo, contract_bytes, contract_identity, requested_logical_path)
        }
        #[cfg(not(unix))]
        {
            let _ = (repo_root, requested_logical_path);
            Err(error("MAPPED_SOURCE_READ_PLATFORM_UNSUPPORTED", "no safe descriptor implementation"))
        }
    }

    pub fn validate(&self, repo_root: &Path) -> Result<(), MappedSourceReadError> {
        #[cfg(unix)]
        {
            let repo = open_absolute_directory(repo_root)?;
            let (bytes, identity) = read_contract(&repo)?;
            if digest(&bytes) != self.contract_sha256 {
                return Err(error("MAPPED_SOURCE_READ_CONTRACT_SHA_MISMATCH", "on-disk contract changed"));
            }
            let observed = derive_observed(repo_root, &repo, &bytes, identity, &self.requested_logical_path)?;
            if observed != *self {
                return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "descriptor does not match current binding"));
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = repo_root;
            Err(error("MAPPED_SOURCE_READ_PLATFORM_UNSUPPORTED", "no safe descriptor implementation"))
        }
    }

    pub fn open_read_only(&self, repo_root: &Path) -> Result<MappedSourceReadGuard, MappedSourceReadError> {
        #[cfg(unix)]
        {
            self.validate(repo_root)?;
            let file = open_bound_target(self)?;
            let guard = MappedSourceReadGuard { binding: self.clone(), file };
            guard.revalidate(repo_root)?;
            Ok(guard)
        }
        #[cfg(not(unix))]
        {
            let _ = repo_root;
            Err(error("MAPPED_SOURCE_READ_PLATFORM_UNSUPPORTED", "no safe descriptor implementation"))
        }
    }
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn relative_parts(raw: &str) -> Result<Vec<&str>, MappedSourceReadError> {
    let parts: Vec<_> = raw.split('/').collect();
    if raw.is_empty() || raw.len() > MAX_CONTRACT_BYTES || raw.contains('\\')
        || raw.chars().any(|character| character.is_control() || matches!(character, '*' | '?' | '[' | ']'))
        || parts.iter().any(|part| matches!(*part, "" | "." | ".."))
    {
        return Err(error("MAPPED_SOURCE_READ_PATH_INVALID", "canonical nonempty relative path required"));
    }
    Ok(parts)
}

fn absolute_parts(path: &Path) -> Result<Vec<&str>, MappedSourceReadError> {
    let raw = path.to_str()
        .ok_or_else(|| error("MAPPED_SOURCE_READ_PATH_INVALID", "UTF-8 path required"))?;
    let suffix = raw.strip_prefix('/')
        .ok_or_else(|| error("MAPPED_SOURCE_READ_PATH_INVALID", "canonical absolute directory required"))?;
    relative_parts(suffix)
}

fn field<'a>(object: &'a Value, name: &str) -> Result<&'a str, MappedSourceReadError> {
    object.get(name).and_then(Value::as_str)
        .ok_or_else(|| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", format!("missing string {name}")))
}

struct UnambiguousJson(Value);

impl<'de> Deserialize<'de> for UnambiguousJson {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct JsonVisitor;

        impl<'de> serde::de::Visitor<'de> for JsonVisitor {
            type Value = UnambiguousJson;

            fn expecting(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("JSON without duplicate object keys")
            }

            fn visit_unit<E>(self) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::Null))
            }

            fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::Bool(value)))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::Number(value.into())))
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::Number(value.into())))
            }

            fn visit_f64<E: serde::de::Error>(self, value: f64) -> Result<Self::Value, E> {
                serde_json::Number::from_f64(value)
                    .map(|number| UnambiguousJson(Value::Number(number)))
                    .ok_or_else(|| E::custom("non-finite JSON number"))
            }

            fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::String(value.to_owned())))
            }

            fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
                Ok(UnambiguousJson(Value::String(value)))
            }

            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut sequence: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(value) = sequence.next_element::<UnambiguousJson>()? {
                    values.push(value.0);
                }
                Ok(UnambiguousJson(Value::Array(values)))
            }

            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut entries: A) -> Result<Self::Value, A::Error> {
                let mut values = serde_json::Map::new();
                while let Some(key) = entries.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(serde::de::Error::custom("duplicate JSON object key"));
                    }
                    values.insert(key, entries.next_value::<UnambiguousJson>()?.0);
                }
                Ok(UnambiguousJson(Value::Object(values)))
            }
        }

        deserializer.deserialize_any(JsonVisitor)
    }
}

#[cfg(unix)]
fn file_identity(file: &File) -> Result<FileSystemIdentity, MappedSourceReadError> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file.metadata()
        .map_err(|cause| error("MAPPED_SOURCE_READ_PATH_INVALID", cause.to_string()))?;
    Ok(FileSystemIdentity { device: metadata.dev(), inode: metadata.ino() })
}

#[cfg(unix)]
fn open_bound_target(binding: &MappedSourceReadBinding) -> Result<File, MappedSourceReadError> {
    let physical_root = open_absolute_directory(&binding.physical_root)?;
    if file_identity(&physical_root)? != binding.physical_root_identity {
        return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "physical root changed before open"));
    }
    let suffix = Path::new(&binding.logical_path)
        .strip_prefix(&binding.logical_namespace)
        .map_err(|_| error("MAPPED_SOURCE_READ_BINDING_CHANGED", "logical suffix"))?;
    let file = open_relative_file(&physical_root, suffix)?;
    let metadata = file.metadata()
        .map_err(|cause| error("MAPPED_SOURCE_READ_TARGET_INVALID", cause.to_string()))?;
    if !metadata.is_file() {
        return Err(error("MAPPED_SOURCE_READ_NOT_REGULAR_FILE", "exact pointer is not a regular file"));
    }
    Ok(file)
}

#[cfg(unix)]
fn open_directory_at(parent: &File, part: &str) -> Result<File, MappedSourceReadError> {
    use rustix::fs::{Mode, OFlags, openat};
    openat(parent, part, OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW, Mode::empty())
        .map(File::from)
        .map_err(|cause| error("MAPPED_SOURCE_READ_PATH_INVALID", format!("directory {part}: {cause}")))
}

#[cfg(unix)]
fn open_absolute_directory(path: &Path) -> Result<File, MappedSourceReadError> {
    use rustix::fs::{CWD, Mode, OFlags, openat};
    let parts = absolute_parts(path)?;
    let mut directory = File::from(openat(CWD, "/", OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW, Mode::empty())
        .map_err(|cause| error("MAPPED_SOURCE_READ_PATH_INVALID", cause.to_string()))?);
    for part in parts {
        directory = open_directory_at(&directory, part)?;
    }
    Ok(directory)
}

#[cfg(unix)]
fn open_relative_file(root: &File, relative: &Path) -> Result<File, MappedSourceReadError> {
    use rustix::fs::{Mode, OFlags, openat};
    let raw = relative.to_str()
        .ok_or_else(|| error("MAPPED_SOURCE_READ_PATH_INVALID", "UTF-8 suffix required"))?;
    let parts = relative_parts(raw)?;
    let mut directory = root.try_clone()
        .map_err(|cause| error("MAPPED_SOURCE_READ_PATH_INVALID", cause.to_string()))?;
    for (index, part) in parts.iter().enumerate() {
        if index + 1 == parts.len() {
            return openat(&directory, *part, OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK, Mode::empty())
                .map(File::from)
                .map_err(|cause| error("MAPPED_SOURCE_READ_TARGET_INVALID", cause.to_string()));
        }
        directory = open_directory_at(&directory, part)?;
    }
    Err(error("MAPPED_SOURCE_READ_PATH_INVALID", "empty suffix"))
}

#[cfg(unix)]
fn read_contract(repo: &File) -> Result<(Vec<u8>, FileSystemIdentity), MappedSourceReadError> {
    use std::os::unix::fs::MetadataExt;
    let mut file = open_relative_file(repo, Path::new(CONTRACT_PATH))?;
    let identity = file_identity(&file)?;
    let metadata = file.metadata()
        .map_err(|cause| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", cause.to_string()))?;
    if !metadata.is_file() {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "contract is not a regular file"));
    }
    if metadata.len() > MAX_CONTRACT_BYTES as u64 {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_TOO_LARGE", "contract byte limit"));
    }
    let mut bytes = Vec::new();
    (&mut file).take(MAX_CONTRACT_BYTES as u64 + 1).read_to_end(&mut bytes)
        .map_err(|cause| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", cause.to_string()))?;
    if bytes.len() > MAX_CONTRACT_BYTES {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_TOO_LARGE", "contract byte limit"));
    }
    if file_identity(&file)? != identity {
        return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "contract descriptor identity"));
    }
    let after = file.metadata()
        .map_err(|cause| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", cause.to_string()))?;
    if (metadata.len(), metadata.mtime(), metadata.mtime_nsec(), metadata.ctime(), metadata.ctime_nsec())
        != (after.len(), after.mtime(), after.mtime_nsec(), after.ctime(), after.ctime_nsec())
    {
        return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "contract changed during read"));
    }
    // Reopen the fixed no-follow path to reject replacement during the read.
    if file_identity(&open_relative_file(repo, Path::new(CONTRACT_PATH))?)? != identity {
        return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "contract path identity"));
    }
    Ok((bytes, identity))
}

#[cfg(unix)]
fn derive_observed(
    repo_root: &Path,
    repo: &File,
    bytes: &[u8],
    contract_identity: FileSystemIdentity,
    requested: &str,
) -> Result<MappedSourceReadBinding, MappedSourceReadError> {
    use rustix::fs::{AtFlags, FileType, readlinkat, statat};
    if bytes.len() > MAX_CONTRACT_BYTES {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_TOO_LARGE", "contract byte limit"));
    }
    let contract = serde_json::from_slice::<UnambiguousJson>(bytes)
        .map_err(|cause| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", cause.to_string()))?.0;
    if field(&contract, "schema_version")? != "dcf_v2_contract_v1"
        || field(&contract, "status")? != "active"
        || field(&contract, "authority_effect")? != "none"
        || contract.get("no_apply").and_then(Value::as_bool) != Some(true)
    {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "active no-apply DCF contract required"));
    }
    let runtime = contract.get("runtime")
        .ok_or_else(|| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "runtime missing"))?;
    let cutover = contract.get("two_tier_storage_cutover")
        .ok_or_else(|| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "storage cutover missing"))?;
    let namespace = field(runtime, "namespace")?;
    let namespace_parts = relative_parts(namespace)?;
    if field(cutover, "logical_runtime_namespace")? != namespace {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "logical namespace mismatch"));
    }
    let logical_path = field(runtime, "current_pointer")?;
    relative_parts(logical_path)?;
    let suffix = Path::new(logical_path).strip_prefix(namespace)
        .map_err(|_| error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "pointer is outside namespace"))?;
    let suffix_text = suffix.to_str()
        .ok_or_else(|| error("MAPPED_SOURCE_READ_PATH_INVALID", "UTF-8 suffix required"))?;
    relative_parts(suffix_text)?;
    let absolute_logical = repo_root.join(logical_path);
    if requested.starts_with('/') {
        absolute_parts(Path::new(requested))?;
    } else {
        relative_parts(requested)?;
    }
    if requested != logical_path && Path::new(requested) != absolute_logical {
        return Err(error("MAPPED_SOURCE_READ_REQUEST_NOT_EXACT", "only configured current_pointer is eligible"));
    }
    let physical_root = PathBuf::from(field(cutover, "target_physical_runtime_root")?);
    absolute_parts(&physical_root)?;
    if physical_root.starts_with(repo_root) {
        return Err(error("MAPPED_SOURCE_READ_CONTRACT_INVALID", "physical materialization must be outside repo"));
    }
    let physical = open_absolute_directory(&physical_root)?;
    let physical_identity = file_identity(&physical)?;
    let mut directory = repo.try_clone()
        .map_err(|cause| error("MAPPED_SOURCE_READ_PATH_INVALID", cause.to_string()))?;
    let mut alias = None;
    for (index, part) in namespace_parts.iter().enumerate() {
        let before = statat(&directory, *part, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|cause| error("MAPPED_SOURCE_READ_ALIAS_INVALID", cause.to_string()))?;
        match FileType::from_raw_mode(before.st_mode) {
            FileType::Directory => directory = open_directory_at(&directory, part)?,
            FileType::Symlink => {
                let link = readlinkat(&directory, *part, Vec::new())
                    .map_err(|cause| error("MAPPED_SOURCE_READ_ALIAS_INVALID", cause.to_string()))?;
                let link_text = link.to_str()
                    .map_err(|_| error("MAPPED_SOURCE_READ_ALIAS_INVALID", "UTF-8 link text required"))?;
                let mut expected = physical_root.clone();
                for descendant in namespace_parts.iter().skip(index + 1).rev() {
                    if expected.file_name().and_then(|name| name.to_str()) != Some(*descendant) {
                        return Err(error("MAPPED_SOURCE_READ_ALIAS_INVALID", "namespace suffix does not match physical root"));
                    }
                    if !expected.pop() {
                        return Err(error("MAPPED_SOURCE_READ_ALIAS_INVALID", "physical namespace ancestor missing"));
                    }
                }
                absolute_parts(&expected)?;
                if Path::new(link_text) != expected || link_text != expected.to_string_lossy() {
                    return Err(error("MAPPED_SOURCE_READ_ALIAS_INVALID", "link text is not the contract-derived namespace ancestor"));
                }
                let after = statat(&directory, *part, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|cause| error("MAPPED_SOURCE_READ_ALIAS_INVALID", cause.to_string()))?;
                if before.st_dev != after.st_dev || before.st_ino != after.st_ino || before.st_mode != after.st_mode {
                    return Err(error("MAPPED_SOURCE_READ_BINDING_CHANGED", "alias changed during inspection"));
                }
                alias = Some((
                    namespace_parts[..=index].join("/"),
                    link_text.to_string(),
                    FileSystemIdentity { device: before.st_dev as u64, inode: before.st_ino as u64 },
                    file_identity(&directory)?,
                ));
                // The remaining namespace ancestry is inspected through the
                // canonical physical root, never by following this alias.
                break;
            }
            _ => return Err(error("MAPPED_SOURCE_READ_ALIAS_INVALID", "namespace ancestor is not directory or declared alias")),
        }
    }
    let (alias_path, alias_link_text, alias_identity, alias_parent_identity) = alias
        .ok_or_else(|| error("MAPPED_SOURCE_READ_ALIAS_INVALID", "mapped namespace has no declared ancestor alias"))?;
    Ok(MappedSourceReadBinding {
        schema_version: BINDING_SCHEMA.to_string(),
        contract_path: CONTRACT_PATH.to_string(),
        contract_sha256: digest(bytes),
        contract_identity,
        repo_identity: file_identity(repo)?,
        requested_logical_path: requested.to_string(),
        logical_path: logical_path.to_string(),
        logical_namespace: namespace.to_string(),
        alias_path,
        alias_link_text,
        alias_identity,
        alias_parent_identity,
        physical_path: physical_root.join(suffix),
        physical_root,
        physical_root_identity: physical_identity,
    })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;

    struct Fixture {
        _tree: tempfile::TempDir,
        repo: PathBuf,
        physical: PathBuf,
        bytes: Vec<u8>,
        logical: String,
    }

    impl Fixture {
        fn new(suffix: &str) -> Self {
            let tree = tempfile::tempdir().expect("fixture root");
            let root = tree.path().canonicalize().expect("canonical fixture");
            let repo = root.join("repo");
            let physical = root.join("external/dcf/v2");
            fs::create_dir_all(repo.join("config/contracts")).expect("contract directory");
            fs::create_dir_all(repo.join("output")).expect("output directory");
            fs::create_dir_all(&physical).expect("physical root");
            let pointer = physical.join(suffix);
            fs::create_dir_all(pointer.parent().expect("pointer parent")).expect("pointer directory");
            fs::write(&pointer, b"{\"generation\":1}\n").expect("pointer");
            symlink(physical.parent().expect("physical parent"), repo.join("output/dcf_runtime")).expect("namespace alias");
            let logical = format!("output/dcf_runtime/v2/{suffix}");
            let bytes = serde_json::to_vec(&serde_json::json!({
                "schema_version": "dcf_v2_contract_v1", "status": "active",
                "authority_effect": "none", "no_apply": true,
                "runtime": { "namespace": "output/dcf_runtime/v2", "current_pointer": logical },
                "two_tier_storage_cutover": {
                    "logical_runtime_namespace": "output/dcf_runtime/v2",
                    "target_physical_runtime_root": physical,
                },
            })).expect("fixture contract");
            fs::write(repo.join(CONTRACT_PATH), &bytes).expect("contract");
            Self { _tree: tree, repo, physical, bytes, logical }
        }

        fn binding(&self) -> MappedSourceReadBinding {
            MappedSourceReadBinding::derive(&self.repo, &self.bytes, &digest(&self.bytes), &self.logical).expect("derive")
        }
    }

    #[test]
    fn mapped_source_read_exact_descriptor_opens_readonly_and_preserves_logical_identity() {
        use rustix::fs::{OFlags, fcntl_getfl};
        let fixture = Fixture::new("current.json");
        let binding = fixture.binding();
        let roundtrip: MappedSourceReadBinding = serde_json::from_value(serde_json::to_value(&binding).expect("serialize")).expect("deserialize");
        roundtrip.validate(&fixture.repo).expect("validate");
        let guard = binding.open_read_only(&fixture.repo).expect("open");
        assert_eq!(guard.requested_logical_path(), fixture.logical);
        assert_eq!(guard.binding().physical_path, fixture.physical.join("current.json"));
        assert_eq!(fcntl_getfl(guard.file()).expect("flags") & OFlags::ACCMODE, OFlags::RDONLY);
        let mut content = String::new();
        guard.file().try_clone().expect("clone").read_to_string(&mut content).expect("read");
        assert_eq!(content, "{\"generation\":1}\n");
        guard.revalidate(&fixture.repo).expect("postcheck");
        let absolute = fixture.repo.join(&fixture.logical).to_string_lossy().into_owned();
        let absolute_binding = MappedSourceReadBinding::derive(&fixture.repo, &fixture.bytes, &digest(&fixture.bytes), &absolute).expect("exact absolute request");
        assert_eq!(absolute_binding.open_read_only(&fixture.repo).expect("absolute open").requested_logical_path(), absolute);
    }

    #[test]
    fn mapped_source_read_rejects_other_alias_sibling_and_noncanonical_requests() {
        let fixture = Fixture::new("current.json");
        symlink(&fixture.physical, fixture.repo.join("other")).expect("other alias");
        for request in ["other/current.json", "output/dcf_runtime/v2/sibling.json", "output/dcf_runtime/v2/../v2/current.json", "./output/dcf_runtime/v2/current.json", "output//dcf_runtime/v2/current.json", "output/dcf_runtime/v2/*.json", ""] {
            assert!(MappedSourceReadBinding::derive(&fixture.repo, &fixture.bytes, &digest(&fixture.bytes), request).is_err(), "{request}");
        }
        let mut tampered = fixture.binding();
        tampered.physical_path = fixture.physical.join("sibling.json");
        assert!(tampered.validate(&fixture.repo).is_err());
        let mut tampered = fixture.binding();
        tampered.physical_root_identity.inode += 1;
        assert!(tampered.validate(&fixture.repo).is_err());
        let mut tampered = fixture.binding();
        tampered.contract_path = "other.json".into();
        assert!(tampered.validate(&fixture.repo).is_err());
        let mut unknown = serde_json::to_value(fixture.binding()).expect("serialize");
        unknown["volume_uuid_verified"] = Value::Bool(true);
        assert!(serde_json::from_value::<MappedSourceReadBinding>(unknown).is_err());
    }

    #[test]
    fn mapped_source_read_rejects_contract_sha_semantics_and_oversize() {
        let fixture = Fixture::new("current.json");
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &fixture.bytes, &"0".repeat(64), &fixture.logical).is_err());
        for (name, value) in [("schema_version", Value::String("other".into())), ("status", Value::String("inactive".into())), ("no_apply", Value::Bool(false)), ("authority_effect", Value::String("write".into()))] {
            let mut contract: Value = serde_json::from_slice(&fixture.bytes).expect("contract JSON");
            contract[name] = value;
            let bytes = serde_json::to_vec(&contract).expect("contract bytes");
            fs::write(fixture.repo.join(CONTRACT_PATH), &bytes).expect("tamper contract");
            assert!(MappedSourceReadBinding::derive(&fixture.repo, &bytes, &digest(&bytes), &fixture.logical).is_err());
        }
        let bytes = vec![b' '; MAX_CONTRACT_BYTES + 1];
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &bytes, &digest(&bytes), &fixture.logical).is_err());
    }

    #[test]
    fn mapped_source_read_rejects_duplicate_contract_keys_at_every_depth() {
        let fixture = Fixture::new("current.json");
        let original = String::from_utf8(fixture.bytes.clone()).expect("fixture JSON");
        let physical = serde_json::to_string(&fixture.physical).expect("physical JSON");
        let logical = serde_json::to_string(&fixture.logical).expect("logical JSON");
        let cases = [
            original.replace("\"authority_effect\":\"none\"", "\"authority_effect\":\"write\",\"authority_effect\":\"none\""),
            original.replace("\"authority_effect\":\"none\"", "\"authority_\\u0065ffect\":\"write\",\"authority_effect\":\"none\""),
            original.replace("\"namespace\":\"output/dcf_runtime/v2\"", "\"namespace\":\"other\",\"namespace\":\"output/dcf_runtime/v2\""),
            original.replace(&format!("\"current_pointer\":{logical}"), &format!("\"current_pointer\":\"other\",\"current_pointer\":{logical}")),
            original.replace(&format!("\"target_physical_runtime_root\":{physical}"), &format!("\"target_physical_runtime_root\":\"/other\",\"target_physical_runtime_root\":{physical}")),
            original.replace("\"no_apply\":true", "\"no_apply\":true,\"extra\":[{\"key\":1,\"key\":2}]"),
        ];
        for (index, text) in cases.into_iter().enumerate() {
            assert_ne!(text, original, "case {index} inserted duplicate fields");
            let bytes = text.as_bytes();
            fs::write(fixture.repo.join(CONTRACT_PATH), bytes).expect("malformed contract");
            let failure = MappedSourceReadBinding::derive(&fixture.repo, bytes, &digest(bytes), &fixture.logical)
                .expect_err("duplicate contract fields must not use last-key-wins");
            assert_eq!(failure.code(), "MAPPED_SOURCE_READ_CONTRACT_INVALID", "case {index}");
        }
    }

    #[test]
    fn mapped_source_read_rejects_contract_target_tamper_and_namespace_mismatch() {
        let fixture = Fixture::new("current.json");
        let binding = fixture.binding();
        let mut contract: Value = serde_json::from_slice(&fixture.bytes).expect("contract JSON");
        contract["two_tier_storage_cutover"]["target_physical_runtime_root"] = Value::String(fixture.repo.to_string_lossy().into_owned());
        let bytes = serde_json::to_vec(&contract).expect("tampered contract");
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &bytes, &digest(&bytes), &fixture.logical).is_err());
        fs::write(fixture.repo.join(CONTRACT_PATH), &bytes).expect("tamper target on disk");
        assert!(binding.validate(&fixture.repo).is_err());
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &bytes, &digest(&bytes), &fixture.logical).is_err());
        let fixture = Fixture::new("current.json");
        let mut contract: Value = serde_json::from_slice(&fixture.bytes).expect("contract JSON");
        contract["two_tier_storage_cutover"]["logical_runtime_namespace"] = Value::String("output/other/v2".into());
        let bytes = serde_json::to_vec(&contract).expect("namespace mismatch");
        fs::write(fixture.repo.join(CONTRACT_PATH), &bytes).expect("tamper namespace");
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &bytes, &digest(&bytes), &fixture.logical).is_err());
    }

    #[test]
    fn mapped_source_read_rejects_final_and_descendant_symlinks_and_nonregular_target() {
        for target_kind in ["symlink", "directory", "socket"] {
            let fixture = Fixture::new("current.json");
            fs::remove_file(fixture.physical.join("current.json")).expect("remove pointer");
            match target_kind {
                "symlink" => { fs::write(fixture.physical.join("other.json"), b"x").expect("other"); symlink("other.json", fixture.physical.join("current.json")).expect("final alias"); }
                "directory" => fs::create_dir(fixture.physical.join("current.json")).expect("directory target"),
                "socket" => { std::os::unix::net::UnixListener::bind(fixture.physical.join("current.json")).expect("socket"); }
                _ => unreachable!(),
            }
            assert!(fixture.binding().open_read_only(&fixture.repo).is_err(), "{target_kind}");
        }
        let fixture = Fixture::new("sub/current.json");
        fs::rename(fixture.physical.join("sub"), fixture.physical.join("real_sub")).expect("move descendant");
        symlink("real_sub", fixture.physical.join("sub")).expect("descendant alias");
        assert!(fixture.binding().open_read_only(&fixture.repo).is_err());
    }

    #[test]
    fn mapped_source_read_guard_rejects_alias_retarget_and_same_text_replacement() {
        for retarget in [false, true] {
            let fixture = Fixture::new("current.json");
            let guard = fixture.binding().open_read_only(&fixture.repo).expect("open");
            let alias = fixture.repo.join("output/dcf_runtime");
            fs::rename(&alias, fixture.repo.join("saved_alias")).expect("preserve alias inode");
            let target = if retarget { fixture.repo.clone() } else { fixture.physical.parent().expect("physical parent").to_path_buf() };
            symlink(target, &alias).expect("replace alias");
            assert!(guard.revalidate(&fixture.repo).is_err());
        }
    }

    #[test]
    fn mapped_source_read_guard_rejects_physical_root_and_contract_replacement() {
        let fixture = Fixture::new("current.json");
        let guard = fixture.binding().open_read_only(&fixture.repo).expect("open");
        fs::rename(&fixture.physical, fixture.physical.with_file_name("old_v2")).expect("move root");
        fs::create_dir(&fixture.physical).expect("replace root");
        fs::write(fixture.physical.join("current.json"), b"replacement").expect("replacement pointer");
        assert!(guard.revalidate(&fixture.repo).is_err());
        let fixture = Fixture::new("current.json");
        let guard = fixture.binding().open_read_only(&fixture.repo).expect("open");
        fs::write(fixture.repo.join(CONTRACT_PATH), b"{}").expect("tamper contract");
        assert_eq!(guard.revalidate(&fixture.repo).expect_err("changed SHA").code(), "MAPPED_SOURCE_READ_CONTRACT_SHA_MISMATCH");
    }

    #[test]
    fn mapped_source_read_guard_rejects_pointer_or_descendant_replacement_after_open() {
        let fixture = Fixture::new("current.json");
        let guard = fixture.binding().open_read_only(&fixture.repo).expect("open");
        fs::rename(fixture.physical.join("current.json"), fixture.physical.join("original.json")).expect("preserve opened pointer");
        fs::write(fixture.physical.join("current.json"), b"new pointer").expect("replace pointer");
        assert!(guard.revalidate(&fixture.repo).is_err());
        let fixture = Fixture::new("sub/current.json");
        let guard = fixture.binding().open_read_only(&fixture.repo).expect("open");
        fs::rename(fixture.physical.join("sub"), fixture.physical.join("original_sub")).expect("move descendant");
        symlink("original_sub", fixture.physical.join("sub")).expect("descendant alias");
        assert!(guard.revalidate(&fixture.repo).is_err());
    }

    #[test]
    fn mapped_source_read_rejects_repo_root_physical_root_and_contract_aliases() {
        let fixture = Fixture::new("current.json");
        let repo_alias = fixture.repo.with_file_name("repo_alias");
        symlink(&fixture.repo, &repo_alias).expect("repo alias");
        assert!(MappedSourceReadBinding::derive(&repo_alias, &fixture.bytes, &digest(&fixture.bytes), &fixture.logical).is_err());
        let contract = fixture.repo.join(CONTRACT_PATH);
        fs::rename(&contract, contract.with_file_name("real.json")).expect("move contract");
        symlink("real.json", &contract).expect("contract alias");
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &fixture.bytes, &digest(&fixture.bytes), &fixture.logical).is_err());
        let fixture = Fixture::new("current.json");
        fs::rename(&fixture.physical, fixture.physical.with_file_name("real_v2")).expect("move physical root");
        symlink("real_v2", &fixture.physical).expect("physical root alias");
        assert!(MappedSourceReadBinding::derive(&fixture.repo, &fixture.bytes, &digest(&fixture.bytes), &fixture.logical).is_err());
    }
}
