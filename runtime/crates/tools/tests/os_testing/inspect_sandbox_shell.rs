//! The inspect profile bound through TURA_INSPECT_SANDBOX_PROFILE confines every shell this
//! process starts. Runs in its own process because the binding is process-wide.
#![cfg(target_os = "macos")]
use code_tools::shell_executor::{ShellKind, execute, focused_verifier_sandbox_available};
use std::fs;

fn profile(evidence: &std::path::Path, scratch: &std::path::Path) -> String {
    format!(
        "(version 1)\n(deny default)\n(allow process-exec) (allow process-fork) (allow signal (target same-sandbox))\n\
         (allow sysctl-read) (allow mach-lookup) (allow ipc-posix-shm) (allow iokit-open) (allow file-ioctl)\n\
         (allow file-read*)\n(allow file-write* (subpath {:?}) (subpath {:?}) (literal \"/dev/null\") (literal \"/dev/tty\"))\n",
        evidence.display().to_string(),
        scratch.display().to_string()
    )
}

#[test]
fn bound_inspect_profile_confines_shell_writes_and_refuses_in_process_patches() {
    if !focused_verifier_sandbox_available() {
        eprintln!("skipped: Seatbelt unavailable or already active");
        return;
    }
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap();
    let inspect = root.join("inspect");
    let (evidence, scratch, outside) = (inspect.join("evidence"), inspect.join("scratch"), root.join("outside"));
    for dir in [&evidence, &scratch, &outside] {
        fs::create_dir_all(dir).unwrap();
    }
    let sb = inspect.join("profile.sb");
    fs::write(&sb, profile(&evidence, &scratch)).unwrap();
    // SAFETY: this integration-test process runs only this test; no other thread reads the environment.
    #[allow(unsafe_code, reason = "Rust 2024 process-environment mutation in a single-test binary")]
    unsafe {
        std::env::set_var("TURA_INSPECT_SANDBOX_PROFILE", &sb);
    }
    let run = |line: String| execute(&line, &root, 30, ShellKind::Zsh);
    let text_of = |response: code_tools::commands::CommandResponse| {
        format!("{} {} {} {}", response.exit_code, response.stdout, response.stderr, response.output)
    };
    let written = run(format!("echo recorded > {}/receipt.json && cat {}/receipt.json && echo $TMPDIR", evidence.display(), evidence.display()));
    assert!(written.success, "{}", text_of(written.clone()));
    let text = text_of(written);
    assert!(text.contains("recorded"), "{text}");
    assert!(text.contains(&scratch.display().to_string()), "TMPDIR is the bound scratch: {text}");
    assert_eq!(fs::read_to_string(evidence.join("receipt.json")).unwrap(), "recorded\n");
    let _ = run(format!("echo escaped > {}/escape.txt", outside.display()));
    assert!(!outside.join("escape.txt").exists(), "writes outside the evidence root are denied");
    let patch = format!("apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: {}/patched.txt\n+x\n*** End Patch\nEOF", outside.display());
    let refused = text_of(run(patch));
    assert!(refused.contains("inspect shell"), "{refused}");
    assert!(!outside.join("patched.txt").exists());
    #[allow(unsafe_code, reason = "Rust 2024 process-environment mutation in a single-test binary")]
    unsafe {
        std::env::remove_var("TURA_INSPECT_SANDBOX_PROFILE");
    }
    let _ = run(format!("echo plain > {}/unbound.txt", outside.display()));
    assert!(outside.join("unbound.txt").exists(), "without a binding the shell is not sandboxed");
}

