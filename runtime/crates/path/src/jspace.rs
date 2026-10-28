//! Local admission and Path x Operation enforcement for an optional DCF
//! J-Space contract.
//!
//! The matcher contains no DCF, provider, network, or LLM dependency. A
//! contract is parsed and digest-checked once by `JSpaceAdmissionCache`; later
//! checks use only the compiled tries and the existing workspace boundary.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::error::Error;
use std::fmt::{Display, Formatter};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use crate::mapped_source_read::MappedSourceReadBinding;

#[path = "jspace_read.rs"]
mod read_commands;
#[path = "jspace_inspect.rs"]
pub mod inspect_shell;
#[path = "jspace_parent_shell.rs"]
pub mod parent_shell;

pub const JSPACE_SCHEMA_VERSION: &str = "jspace_contract_v3";
const JSPACE_SINGLE_ROOT_SCHEMA_VERSION: &str = "jspace_contract_v2";
const JSPACE_LEGACY_SCHEMA_VERSION: &str = "jspace_contract_v1";
const JSPACE_AUTHORIZATION_SCHEMA_VERSION: &str = "jspace_authorization_v1";
const JSPACE_EFFECT_AUTHORIZATION_SCHEMA_VERSION: &str = "jspace_effect_authorization_v1";
pub const JSPACE_EXPANSION_REQUIRED: &str = "JSPACE_EXPANSION_REQUIRED";

const KNOWN_OPERATIONS: &[&str] = &[
    "read",
    "create",
    "modify",
    "delete",
    "command",
    "network",
    "install",
    "system_mutation",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JSpaceError {
    code: String,
    operation: String,
    target: String,
    detail: String,
}

impl JSpaceError {
    pub fn new(
        code: impl Into<String>,
        operation: &str,
        target: &str,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            code: code.into(),
            operation: operation.to_string(),
            target: target.to_string(),
            detail: detail.into(),
        }
    }

    pub fn code(&self) -> &str {
        &self.code
    }

    pub fn operation(&self) -> &str {
        &self.operation
    }

    pub fn target(&self) -> &str {
        &self.target
    }
}

impl Display for JSpaceError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "{}: operation={} target={} detail={}",
            self.code,
            if self.operation.is_empty() {
                "<none>"
            } else {
                &self.operation
            },
            if self.target.is_empty() {
                "<none>"
            } else {
                &self.target
            },
            self.detail
        )
    }
}

impl Error for JSpaceError {}

#[derive(Clone, Debug)]
struct PathTrieNode {
    children: HashMap<String, usize>,
    exact: bool,
    recursive: bool,
}

#[derive(Clone, Debug, Default)]
struct PathTrie {
    nodes: Vec<PathTrieNode>,
}

impl PathTrie {
    fn new() -> Self {
        Self {
            nodes: vec![PathTrieNode {
                children: HashMap::new(),
                exact: false,
                recursive: false,
            }],
        }
    }

    fn insert(&mut self, raw_scope: &str) -> Result<(), JSpaceError> {
        let (components, recursive) = scope_components(raw_scope)?;
        let mut node_index = 0;
        for component in components {
            let next_index = if let Some(index) = self.nodes[node_index].children.get(&component) {
                *index
            } else {
                let index = self.nodes.len();
                self.nodes.push(PathTrieNode {
                    children: HashMap::new(),
                    exact: false,
                    recursive: false,
                });
                self.nodes[node_index].children.insert(component, index);
                index
            };
            node_index = next_index;
        }
        if recursive {
            self.nodes[node_index].recursive = true;
        } else {
            self.nodes[node_index].exact = true;
        }
        Ok(())
    }

    fn matches(&self, relative: &str) -> bool {
        let mut node_index = 0;
        if self.nodes[node_index].recursive {
            return true;
        }
        for component in relative.split('/').filter(|part| !part.is_empty()) {
            let Some(next_index) = self.nodes[node_index].children.get(component) else {
                return false;
            };
            node_index = *next_index;
            if self.nodes[node_index].recursive {
                return true;
            }
        }
        self.nodes[node_index].exact
    }

    fn matches_exact(&self, relative: &str) -> bool {
        let mut node_index = 0;
        for component in relative.split('/').filter(|part| !part.is_empty()) {
            let Some(next_index) = self.nodes[node_index].children.get(component) else {
                return false;
            };
            node_index = *next_index;
        }
        self.nodes[node_index].exact
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JSpaceScopeProjection {
    pub read_scopes: Vec<String>,
    pub write_scopes: Vec<String>,
}

pub fn canonical_scope_projection(
    read_scopes: &[String],
    write_scopes: &[String],
) -> Result<JSpaceScopeProjection, JSpaceError> {
    Ok(JSpaceScopeProjection {
        read_scopes: canonical_claim_scope_set(read_scopes.to_vec())?,
        write_scopes: canonical_claim_scope_set(write_scopes.to_vec())?,
    })
}

pub fn canonical_declared_targets(targets: &[String]) -> Result<Vec<String>, JSpaceError> {
    if let Some(target) = targets.iter().find(|target| {
        target.contains('*') || target.contains('?') || target.contains('[') || target.contains(']')
    }) {
        return Err(JSpaceError::new(
            "JSPACE_TARGET_INVALID",
            "admission",
            target,
            "declared targets must be exact paths without wildcards",
        ));
    }
    canonical_claim_scope_set(targets.to_vec())
}

pub fn scope_claims_overlap(left_scope: &str, right_scope: &str) -> Result<bool, JSpaceError> {
    let (left_absolute, left_components, left_recursive) = claim_scope_components(left_scope)?;
    let (right_absolute, right_components, right_recursive) = claim_scope_components(right_scope)?;

    if left_absolute != right_absolute {
        return Err(JSpaceError::new(
            "JSPACE_SCOPE_REPRESENTATION_MISMATCH",
            "admission",
            left_scope,
            "absolute and repo-relative scope claims cannot be compared losslessly",
        ));
    }

    if left_components == right_components {
        return Ok(true);
    }
    Ok(
        (left_recursive && right_components.starts_with(&left_components))
            || (right_recursive && left_components.starts_with(&right_components)),
    )
}

pub fn scope_projections_conflict(
    left: &JSpaceScopeProjection,
    right: &JSpaceScopeProjection,
) -> Result<bool, JSpaceError> {
    for left_write in &left.write_scopes {
        for right_scope in right.read_scopes.iter().chain(&right.write_scopes) {
            if scope_claims_overlap(left_write, right_scope)? {
                return Ok(true);
            }
        }
    }
    for right_write in &right.write_scopes {
        for left_read in &left.read_scopes {
            if scope_claims_overlap(right_write, left_read)? {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CommandTarget {
    domain_id: Option<String>,
    operation: String,
    path: String,
    argv_index: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CommandTemplate {
    argv: Vec<String>,
    effects: Vec<String>,
    targets: Vec<CommandTarget>,
}

#[derive(Clone, Debug)]
struct EffectCapabilityMatcher {
    domain_id: String,
    root: PathBuf,
    lexical_root: PathBuf,
    read_projection: Vec<String>,
    write_projection: Vec<String>,
    declared_projection: Vec<String>,
    read_scopes: PathTrie,
    write_scopes: PathTrie,
    declared_targets: PathTrie,
    allowed_operations: HashSet<String>,
}

#[derive(Clone, Debug)]
pub struct JSpaceMatcher {
    schema_version: String,
    repo_root: PathBuf,
    lexical_repo_root: PathBuf,
    content_digest: String,
    authorization_digest: String,
    scope_projection: JSpaceScopeProjection,
    read_scopes: PathTrie,
    write_scopes: PathTrie,
    declared_targets: PathTrie,
    allowed_operations: HashSet<String>,
    denied_operations: HashSet<String>,
    command_templates: Vec<CommandTemplate>,
    command_template_index: HashMap<Vec<String>, usize>,
    read_commands: Option<Value>,
    inspect_shell: Option<Value>,
    parent_shell: Option<Value>,
    verifier_commands: Vec<VerifierCommand>,
    verifier_artifact_root: Option<PathBuf>,
    source_read: bool,
    mapped_source_reads: Vec<MappedSourceReadBinding>,
    directory_source_read: bool,
    effect_capabilities: Vec<EffectCapabilityMatcher>,
    declared_target_projection: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierPinnedFile {
    pub path: PathBuf,
    pub sha256: String,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_verifier_pin_scope"
    )]
    pub scope: Option<VerifierPinScope>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifierPinScope {
    VerifierOnly,
}

fn deserialize_verifier_pin_scope<'de, D>(deserializer: D) -> Result<Option<VerifierPinScope>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Only omission retains the workspace-read pin; null cannot select a scope.
    let scope = String::deserialize(deserializer)?;
    match scope.as_str() {
        "verifier_only" => Ok(Some(VerifierPinScope::VerifierOnly)),
        _ => Err(serde::de::Error::unknown_variant(&scope, &["verifier_only"])),
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierCommand {
    pub argv: Vec<String>,
    pub executable_sha256: String,
    pub pinned_files: Vec<VerifierPinnedFile>,
    pub timeout_seconds: u64,
    pub scratch_root: PathBuf,
    pub network: bool,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_python_import_roots"
    )]
    pub python_import_roots: Option<Vec<PathBuf>>,
    /// V318: the directory the verifier starts in; omitted means the workspace root.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_verifier_cwd"
    )]
    pub cwd: Option<PathBuf>,
}

fn deserialize_python_import_roots<'de, D>(
    deserializer: D,
) -> Result<Option<Vec<PathBuf>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Only omission means None; an explicit null is not a list of roots.
    Vec::<PathBuf>::deserialize(deserializer).map(Some)
}

fn deserialize_verifier_cwd<'de, D>(deserializer: D) -> Result<Option<PathBuf>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Only omission means the workspace root; an explicit null is not a directory.
    PathBuf::deserialize(deserializer).map(Some)
}

impl JSpaceMatcher {
    pub fn from_value(session_root: &Path, contract: &Value) -> Result<Self, JSpaceError> {
        let object = contract.as_object().ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                "",
                "contract must be a JSON object",
            )
        })?;
        let schema_version = required_string(object, "schema_version")?;
        if schema_version != JSPACE_SCHEMA_VERSION
            && schema_version != JSPACE_SINGLE_ROOT_SCHEMA_VERSION
            && schema_version != JSPACE_LEGACY_SCHEMA_VERSION
        {
            return Err(JSpaceError::new(
                "JSPACE_SCHEMA_VERSION_UNSUPPORTED",
                "admission",
                "",
                format!(
                    "expected {JSPACE_SCHEMA_VERSION}, {JSPACE_SINGLE_ROOT_SCHEMA_VERSION}, or {JSPACE_LEGACY_SCHEMA_VERSION}, got {schema_version}"
                ),
            ));
        }
        let mapped_source_reads = parse_mapped_source_read_grant(object)?;
        let (content_digest, authorization_digest) =
            verified_contract_digests(contract, object, &schema_version)?;

        let contract_root = PathBuf::from(required_string(object, "repo_root")?);
        let lexical_repo_root = session_root.to_path_buf();
        let session_root = normalized_root(session_root)?;
        let contract_root = normalized_root(&contract_root)?;
        if contract_root != session_root {
            return Err(JSpaceError::new(
                "JSPACE_ROOT_MISMATCH",
                "admission",
                &contract_root.display().to_string(),
                format!("session root is {}", session_root.display()),
            ));
        }
        if schema_version != JSPACE_LEGACY_SCHEMA_VERSION {
            let generation_root = object
                .get("dcf_generation")
                .and_then(Value::as_object)
                .and_then(|generation| generation.get("repo_root"))
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    JSpaceError::new(
                        "JSPACE_CONTRACT_MALFORMED",
                        "admission",
                        "dcf_generation.repo_root",
                        "DCF evidence root is missing",
                    )
                })?;
            let generation_root = normalized_root(Path::new(generation_root))?;
            if generation_root != contract_root {
                return Err(JSpaceError::new(
                    "JSPACE_DCF_ROOT_MISMATCH",
                    "admission",
                    &generation_root.display().to_string(),
                    format!("contract root is {}", contract_root.display()),
                ));
            }
        }
        validate_expansion_rule(object)?;
        validate_object_field(object, "dcf_generation")?;
        validate_object_field(object, "provenance")?;
        validate_string_array_field(object, "matched_surface_ids")?;
        validate_object_array_field(object, "focused_verifiers")?;
        let allowed_values = required_string_array(object, "allowed_operations")?;
        let denied_values = required_string_array(object, "denied_operations")?;
        let allowed_operations = operation_set(allowed_values, "allowed_operations")?;
        let denied_operations = operation_set(denied_values, "denied_operations")?;
        let source_read = source_read_grant(object)?;
        let (verifier_artifact_root, verifier_commands) =
            parse_verifier_grant(object, &session_root, &schema_version)?;
        if !verifier_commands.is_empty()
            && (!allowed_operations.contains("read")
                || !allowed_operations.contains("command")
                || denied_operations.contains("read")
                || denied_operations.contains("command")
                || !denied_operations.contains("network")
                || allowed_operations.contains("network"))
        {
            return Err(JSpaceError::new(
                "JSPACE_VERIFIER_OPERATION_DENIED",
                "admission",
                "verifier_commands",
                "v2 verifiers require read and command allowed and network denied",
            ));
        }
        if allowed_operations
            .iter()
            .any(|operation| denied_operations.contains(operation))
        {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_CONFLICT",
                "admission",
                "",
                "an operation cannot be both allowed and denied",
            ));
        }
        let effect_capabilities = if schema_version == JSPACE_SCHEMA_VERSION {
            for legacy_field in ["read_scopes", "write_scopes", "declared_targets"] {
                if object.contains_key(legacy_field) {
                    return Err(JSpaceError::new(
                        "JSPACE_CAPABILITY_REPRESENTATION_CONFLICT",
                        "admission",
                        legacy_field,
                        "multi-domain contracts must not retain single-root capability fields",
                    ));
                }
            }
            if allowed_operations.iter().any(|operation| {
                matches!(operation.as_str(), "read" | "create" | "modify" | "delete")
            }) {
                return Err(JSpaceError::new(
                    "JSPACE_CAPABILITY_PERMISSION_INVALID",
                    "admission",
                    "allowed_operations",
                    "path permissions must be declared inside an exact effect domain",
                ));
            }
            parse_effect_capabilities(object, &denied_operations)?
        } else {
            Vec::new()
        };
        let (canonical_read_scopes, canonical_write_scopes, declared_target_projection) =
            if schema_version == JSPACE_SCHEMA_VERSION {
                effect_capability_projection(&effect_capabilities)?
            } else {
                validate_string_array_field(object, "declared_targets")?;
                (
                    canonical_scope_set(required_string_array(object, "read_scopes")?)?,
                    canonical_scope_set(required_string_array(object, "write_scopes")?)?,
                    canonical_declared_targets(&required_string_array(
                        object,
                        "declared_targets",
                    )?)?,
                )
            };
        let mut read_scopes = PathTrie::new();
        if schema_version != JSPACE_SCHEMA_VERSION {
            for scope in &canonical_read_scopes {
                read_scopes.insert(scope)?;
            }
        }
        let mut write_scopes = PathTrie::new();
        if schema_version != JSPACE_SCHEMA_VERSION {
            for scope in &canonical_write_scopes {
                write_scopes.insert(scope)?;
            }
        }
        let mut declared_targets = PathTrie::new();
        if schema_version != JSPACE_SCHEMA_VERSION {
            for target in required_string_array(object, "declared_targets")? {
                if target.contains('*')
                    || target.contains('?')
                    || target.contains('[')
                    || target.contains(']')
                {
                    return Err(JSpaceError::new(
                        "JSPACE_TARGET_INVALID",
                        "admission",
                        &target,
                        "declared targets must be exact paths without wildcards",
                    ));
                }
                declared_targets.insert(&target)?;
            }
        }
        let command_templates = if schema_version != JSPACE_LEGACY_SCHEMA_VERSION {
            parse_command_templates(object, schema_version == JSPACE_SCHEMA_VERSION)?
        } else {
            required_string_array(object, "command_prefixes")?
                .into_iter()
                .map(|command| {
                    Ok(CommandTemplate {
                        argv: parse_shell_argv(&command)?,
                        effects: vec!["read".to_string()],
                        targets: Vec::new(),
                    })
                })
                .collect::<Result<Vec<_>, JSpaceError>>()?
        };
        validate_command_templates(
            &command_templates,
            &allowed_operations,
            &denied_operations,
            &declared_targets,
            &effect_capabilities,
            schema_version == JSPACE_SCHEMA_VERSION,
        )?;
        let read_commands = object.get("read_commands").cloned();
        if let Some(policy) = &read_commands {
            if schema_version != JSPACE_SINGLE_ROOT_SCHEMA_VERSION
                || !allowed_operations.contains("read")
                || !allowed_operations.contains("command")
                || denied_operations.contains("read")
                || denied_operations.contains("command")
            {
                return Err(JSpaceError::new(
                    "JSPACE_READ_COMMAND_DENIED",
                    "admission",
                    "",
                    "read/command v2 grants required",
                ));
            }
            for root in read_commands::validate(policy)? {
                if !canonical_read_scopes.contains(&format!("{root}/**")) {
                    return Err(JSpaceError::new(
                        "JSPACE_READ_COMMAND_DENIED",
                        "admission",
                        &root,
                        "recursive read scope required",
                    ));
                }
                let path = session_root.join(&root);
                if !path.is_dir() || path.canonicalize().ok().as_deref() != Some(path.as_path()) {
                    return Err(JSpaceError::new(
                        "JSPACE_READ_COMMAND_DENIED",
                        "admission",
                        &root,
                        "canonical directory required",
                    ));
                }
            }
        }
        let inspect_shell = object.get("inspect_shell").cloned();
        if let Some(policy) = &inspect_shell {
            inspect_shell::validate(policy)?;
            // Read-and-record only: repository effects keep their exact grants elsewhere.
            if schema_version != JSPACE_SINGLE_ROOT_SCHEMA_VERSION
                || !allowed_operations.contains("read")
                || !allowed_operations.contains("command")
                || allowed_operations
                    .iter()
                    .any(|operation| !matches!(operation.as_str(), "read" | "command"))
                || !canonical_write_scopes.is_empty()
                || read_commands.is_some()
                || !verifier_commands.is_empty()
            {
                return Err(JSpaceError::new(
                    "JSPACE_INSPECT_DENIED",
                    "admission",
                    "inspect_shell",
                    "inspect_shell requires a v2 read/command-only contract without write scopes, discovery or verifiers",
                ));
            }
        }
        let parent_shell = object.get("parent_shell").cloned();
        if let Some(policy) = &parent_shell {
            parent_shell::validate(policy)?;
            // Parent-executed and read-only: a writing action keeps apply_patch as its only edit
            // route, and the in-engine inspect shell is never replaced by it.
            if inspect_shell.is_some()
                || !allowed_operations.contains("read")
                || !allowed_operations.contains("command")
            {
                return Err(JSpaceError::new(
                    "JSPACE_PARENT_SHELL_DENIED",
                    "admission",
                    "parent_shell",
                    "parent_shell requires read and command operations and excludes inspect_shell",
                ));
            }
        }
        // A range is a subset of an already admitted cat read. Keep that
        // per-file boundary instead of granting arbitrary recursive reads.
        let directory_source_read = schema_version == JSPACE_SINGLE_ROOT_SCHEMA_VERSION
            && read_commands.is_some();
        if source_read {
            let scoped_read = if schema_version == JSPACE_SCHEMA_VERSION {
                effect_capabilities.iter().any(|capability| {
                    capability.allowed_operations.contains("read")
                        && capability
                            .read_projection
                            .iter()
                            .any(|scope| !scope.ends_with("/**"))
                })
            } else {
                allowed_operations.contains("read")
                    && canonical_read_scopes
                        .iter()
                        .any(|scope| !scope.ends_with("/**"))
            };
            if !allowed_operations.contains("command")
                || denied_operations.contains("command")
                || denied_operations.contains("read")
                || (!scoped_read && !directory_source_read)
            {
                return Err(JSpaceError::new(
                    "JSPACE_SOURCE_READ_DENIED",
                    "read",
                    "source_read",
                    "source_read requires command and a scoped read grant",
                ));
            }
        }
        for binding in &mapped_source_reads {
            binding.validate(&session_root).map_err(|error| {
                JSpaceError::new(
                    "JSPACE_MAPPED_SOURCE_READ_BINDING_INVALID",
                    "admission",
                    &binding.requested_logical_path,
                    error.to_string(),
                )
            })?;
        }
        if allowed_operations.contains("command")
            && command_templates.is_empty()
            && read_commands.is_none()
            && inspect_shell.is_none()
            && parent_shell.is_none()
            && !source_read
            && verifier_commands.is_empty()
        {
            return Err(JSpaceError::new(
                "JSPACE_COMMAND_TEMPLATE_MISSING",
                "command",
                "",
                "command operation requires at least one exact argv template",
            ));
        }

        // Retain the earliest template if a legacy contract contains repeated argv.
        let mut command_template_index = HashMap::with_capacity(command_templates.len());
        for (index, template) in command_templates.iter().enumerate() {
            command_template_index.entry(template.argv.clone()).or_insert(index);
        }

        let matcher = Self {
            schema_version,
            repo_root: session_root,
            lexical_repo_root,
            content_digest,
            authorization_digest,
            scope_projection: JSpaceScopeProjection {
                read_scopes: canonical_read_scopes,
                write_scopes: canonical_write_scopes,
            },
            read_scopes,
            write_scopes,
            declared_targets,
            allowed_operations,
            denied_operations,
            command_templates,
            command_template_index,
            read_commands,
            inspect_shell,
            parent_shell,
            verifier_commands,
            verifier_artifact_root,
            source_read,
            mapped_source_reads,
            directory_source_read,
            effect_capabilities,
            declared_target_projection,
        };
        for command in &matcher.verifier_commands {
            for pinned in &command.pinned_files {
                matcher.revalidate_verifier_pin(command, pinned, false)?;
            }
            verifier_python_import_roots(command, &matcher)?;
            verifier_cwd(command)?;
        }
        Ok(matcher)
    }

    pub fn semantic_sha256(&self) -> &str {
        &self.authorization_digest
    }

    pub fn authorization_semantic_sha256(&self) -> &str {
        &self.authorization_digest
    }

    pub fn content_sha256(&self) -> &str {
        &self.content_digest
    }

    pub fn repo_root(&self) -> &Path {
        &self.repo_root
    }

    /// An admitted read-and-record contract. Admission already required v2, read/command only,
    /// no write scopes, discovery or verifiers, and a digest-bound Seatbelt profile that confines
    /// every shell write to the task-local evidence root.
    pub fn inspect_shell_admitted(&self) -> bool {
        self.inspect_shell.is_some()
    }

    /// An admitted parent-executed read-only shell (see the `parent_shell` module).
    pub fn parent_shell_admitted(&self) -> bool {
        self.parent_shell.is_some()
    }

    /// The parent executes every shell line except an exact command template, which keeps its
    /// in-engine grant and effects.
    pub fn parent_shell_routes(&self, command_type: &str, command_line: &str) -> bool {
        self.parent_shell.is_some()
            && normalize_command_type(command_type) == "shell_command"
            && !self.shell_template_matches(command_line)
    }

    fn shell_template_matches(&self, command_line: &str) -> bool {
        let (command, _) = shell_command_parts(command_line);
        parse_shell_argv(&command).is_ok_and(|argv| self.command_template_index.contains_key(&argv))
    }

    pub fn source_read_enabled(&self) -> bool {
        self.source_read || self.directory_source_read
    }

    /// Resolve existing read-only pins; the reader must still hash its actual buffer.
    pub fn admitted_source_read_digest(&self, relative: &Path) -> Result<String, JSpaceError> {
        let unavailable = || JSpaceError::new(
            "JSPACE_SOURCE_DIGEST_REFERENCE_UNAVAILABLE", "read",
            &relative.display().to_string(), "requires one admitted exact read-only file pin",
        );
        let name = relative.to_str().ok_or_else(unavailable)?;
        if !self.source_read || relative.is_absolute() || name.contains('\\')
            || relative.components().any(|c| !matches!(c, std::path::Component::Normal(_)))
            || !self.scope_projection.read_scopes.iter().any(|scope| scope == name)
            || self.scope_projection.write_scopes.iter().any(|scope|
                scope_claims_overlap(name, scope).unwrap_or(true))
            || self.mapped_source_read_binding(relative).is_some()
        {
            return Err(unavailable());
        }
        let target = self.repo_root.join(relative);
        self.check_source_read(&target)?;
        let mut digest = None;
        for pin in self.verifier_commands.iter().flat_map(|command| &command.pinned_files) {
            if pin.scope.is_some() || pin.path != target { continue; }
            let value = pin.sha256.to_ascii_lowercase();
            if value.len() != 64 || !value.bytes().all(|b| b.is_ascii_hexdigit())
                || digest.as_ref().is_some_and(|previous| previous != &value)
            {
                return Err(unavailable());
            }
            digest = Some(value);
        }
        digest.ok_or_else(unavailable)
    }

    pub fn mapped_source_reads(&self) -> &[MappedSourceReadBinding] {
        &self.mapped_source_reads
    }

    pub fn mapped_source_read_binding(&self, target: &Path) -> Option<&MappedSourceReadBinding> {
        self.mapped_source_reads.iter().find(|binding| {
            target.to_str() == Some(binding.requested_logical_path.as_str())
        })
    }

    pub fn revalidate_mapped_source_reads(&self) -> Result<(), JSpaceError> {
        for binding in &self.mapped_source_reads {
            binding.validate(&self.repo_root).map_err(|error| {
                JSpaceError::new(
                    "JSPACE_MAPPED_SOURCE_READ_BINDING_INVALID",
                    "read",
                    &binding.requested_logical_path,
                    error.to_string(),
                )
            })?;
        }
        Ok(())
    }

    pub fn verifier_artifact_root(&self) -> Option<&Path> {
        self.verifier_artifact_root.as_deref()
    }

    pub fn verifier_commands(&self) -> &[VerifierCommand] {
        &self.verifier_commands
    }

    pub fn check_verifier_command(&self, index: usize) -> Result<&VerifierCommand, JSpaceError> {
        self.check_operation("command", "focused_verifier")?;
        self.verifier_commands.get(index).ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_VERIFIER_NOT_GRANTED",
                "command",
                &index.to_string(),
                "verifier index is not in the admitted exact grant",
            )
        })
    }

    pub fn revalidate_verifier_paths(&self) -> Result<(), JSpaceError> {
        let Some(root) = self.verifier_artifact_root.as_deref() else {
            return Ok(());
        };
        verifier_path_without_symlink(root, true)?;
        if !root.is_dir() || root.canonicalize().ok().as_deref() != Some(root) {
            return Err(verifier_error(
                "JSPACE_VERIFIER_ARTIFACT_ROOT_INVALID",
                root,
                "artifact root identity changed",
            ));
        }
        for command in &self.verifier_commands {
            let executable = Path::new(&command.argv[0]);
            verifier_file(executable, &command.executable_sha256, true, true, false)?;
            for pinned in &command.pinned_files {
                self.revalidate_verifier_pin(command, pinned, true)?;
            }
            verifier_python_import_roots(command, self)?;
            verifier_cwd(command)?;
            verifier_scratch(&command.scratch_root, root, &self.repo_root, true)?;
        }
        Ok(())
    }

    fn revalidate_verifier_pin(
        &self,
        command: &VerifierCommand,
        pin: &VerifierPinnedFile,
        stale: bool,
    ) -> Result<(), JSpaceError> {
        if pin.scope.is_some() {
            verifier_path_without_symlink(&pin.path, true)?;
            if pin.path.starts_with(&self.repo_root)
                || pin.path.components().any(|part| part.as_os_str() == ".git")
                || self.verifier_commands.iter().any(|command| pin.path.starts_with(&command.scratch_root))
                || pin.path.canonicalize().ok().as_deref() != Some(pin.path.as_path())
            {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_PIN_SCOPE_INVALID",
                    &pin.path,
                    "verifier_only pin must be canonical, outside workspace, Git metadata and every verifier scratch",
                ));
            }
        } else {
            self.check_path("read", &pin.path)?;
        }
        let executable_in_scratch = self.verifier_commands.iter().any(|grant|
            Path::new(&command.argv[0]).starts_with(&grant.scratch_root));
        // A verifier_only Node pin names the program Node runs: directly, with no flags, module
        // lookup or alternate entry before it, outside credential directories and every scratch.
        if pin.scope == Some(VerifierPinScope::VerifierOnly)
            && Path::new(&command.argv[0]).file_name().and_then(|name| name.to_str()) == Some("node")
            && (executable_in_scratch
                || direct_node_verifier_program(&command.argv) != Some(pin.path.as_path()))
        {
            return Err(verifier_error(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                &pin.path,
                "verifier_only Node entry requires a direct JavaScript program",
            ));
        }
        let header_is_data = verifier_interpreter_reads_entries(&command.argv) && !executable_in_scratch;
        verifier_file(&pin.path, &pin.sha256, false, stale, header_is_data)
    }

    pub fn scope_projection(&self) -> &JSpaceScopeProjection {
        &self.scope_projection
    }

    pub fn declared_targets(&self) -> &[String] {
        &self.declared_target_projection
    }

    pub fn check_path(&self, operation: &str, target: &Path) -> Result<(), JSpaceError> {
        if self.schema_version == JSPACE_SCHEMA_VERSION {
            return self.check_effect_path(operation, target, None);
        }
        self.check_operation(operation, &target.display().to_string())?;
        let relative = self.resolve_target(target, operation)?;
        let scope_allowed = if operation == "read" {
            self.read_scopes.matches(&relative)
        } else {
            if operation == "delete" && !self.allowed_operations.contains("delete") {
                return Err(JSpaceError::new(
                    "JSPACE_DELETE_NOT_GRANTED",
                    operation,
                    &target.display().to_string(),
                    "delete requires an explicit delete operation grant",
                ));
            }
            self.write_scopes.matches(&relative)
        };
        if !scope_allowed {
            return Err(JSpaceError::new(
                JSPACE_EXPANSION_REQUIRED,
                operation,
                &target.display().to_string(),
                "exact target is inside the root but outside the declared scope",
            ));
        }
        if operation != "read" && !self.declared_targets.matches(&relative) {
            return Err(JSpaceError::new(
                JSPACE_EXPANSION_REQUIRED,
                operation,
                &target.display().to_string(),
                "mutation target is not one of the exact declared targets",
            ));
        }
        Ok(())
    }

    /// Classify only the existing pinned-rg discovery grant. This grants no execution.
    /// Use the admission parser and read policy, never a shell/basename heuristic.
    pub fn check_read_search_command(
        &self,
        command_type: &str,
        command_line: &str,
    ) -> Result<(), JSpaceError> {
        let kind = normalize_command_type(command_type);
        self.check_operation("command", &kind)?;
        let denied = || {
            JSpaceError::new(
                "JSPACE_READ_COMMAND_DENIED",
                "read",
                "rg",
                "not a pinned direct rg discovery command",
            )
        };
        if !matches!(kind.as_str(), "shell_command" | "bash" | "zsh")
            || self.inspect_shell.is_some()
            || command_line.len() > 8192
        {
            return Err(denied());
        }
        // Plain argv only: JSON wrappers/cwd overrides and shell control remain refused.
        let argv = parse_shell_argv(command_line)?;
        let policy = self.read_commands.as_ref().ok_or_else(denied)?;
        if policy["rg"]["path"].as_str() != argv.first().map(String::as_str) {
            return Err(denied());
        }
        read_commands::check(self, &argv, &self.repo_root)
    }

    pub fn check_command(&self, command_type: &str, command_line: &str) -> Result<(), JSpaceError> {
        let command_type = normalize_command_type(command_type);
        if !is_known_command_type(&command_type) {
            return Err(JSpaceError::new(
                "JSPACE_UNKNOWN_TOOL",
                "command",
                &command_type,
                "command type is not in the local J-Space tool set",
            ));
        }
        // These are not shell commands. Patch paths are checked by the router
        // against create/modify/delete grants after the patch is parsed.
        if matches!(
            command_type.as_str(),
            "apply_patch" | "planning" | "task_status"
        ) {
            if self.source_read
                && command_type == "apply_patch"
                && !self.has_declared_patch_target()
            {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_DENIED",
                    "command",
                    &command_type,
                    "source_read does not authorize an unscoped apply_patch",
                ));
            }
            return Ok(());
        }
        self.check_operation("command", &command_type)?;
        match command_type.as_str() {
            "shell_command" | "bash" | "zsh" => {
                if let Some(policy) = &self.inspect_shell {
                    // The bound Seatbelt profile, not argv parsing, is the effect boundary.
                    inspect_shell::check_binding(policy)?;
                    return Ok(());
                }
                // The parent's bound read-only profile is the effect boundary. The router sends the
                // line over the parent channel and refuses it when no channel is inherited.
                if self.parent_shell.is_some() && !self.shell_template_matches(command_line) {
                    return Ok(());
                }
                let (command, workdir) = shell_command_parts(command_line);
                let argv = parse_shell_argv(&command)?;
                let template_index = self.command_template_index.get(&argv).copied();
                if self.read_commands.is_some() && template_index.is_none() {
                    let cwd = match workdir {
                        Some(ref directory) => {
                            self.resolve_target(Path::new(directory), "read")?;
                            if Path::new(directory).is_absolute() {
                                PathBuf::from(directory)
                            } else {
                                self.repo_root.join(directory)
                            }
                        }
                        None => self.repo_root.clone(),
                    };
                    return read_commands::check(self, &argv, &cwd);
                }
                let template = template_index
                    .map(|index| &self.command_templates[index])
                    .ok_or_else(|| {
                        JSpaceError::new(
                            "JSPACE_COMMAND_DENIED",
                            "command",
                            &command,
                            "command does not match an exact admitted argv template",
                        )
                    })?;
                if let Some(workdir) = workdir {
                    self.resolve_target(Path::new(&workdir), "command")?;
                }
                if self.schema_version == JSPACE_SCHEMA_VERSION {
                    for target in &template.targets {
                        self.check_effect_path(
                            &target.operation,
                            Path::new(&target.path),
                            target.domain_id.as_deref(),
                        )?;
                    }
                } else {
                    for effect in &template.effects {
                        self.check_operation(effect, &command)?;
                    }
                    for target in &template.targets {
                        self.check_path(&target.operation, Path::new(&target.path))?;
                    }
                }
                Ok(())
            }
            "source_read" => Err(JSpaceError::new(
                "JSPACE_SOURCE_READ_INVALID",
                "read",
                "source_read",
                "source_read requires an exact target preflight",
            )),
            "web_discover" => Err(JSpaceError::new(
                "JSPACE_NETWORK_DENIED",
                "network",
                &command_type,
                "network command is not admitted by J-Space",
            )),
            "generate_media" => Err(JSpaceError::new(
                "JSPACE_INSTALL_DENIED",
                "install",
                &command_type,
                "external media command is not admitted by J-Space",
            )),
            "read_media" => Err(JSpaceError::new(
                "JSPACE_COMMAND_DENIED",
                "command",
                &command_type,
                "external command is not admitted by J-Space",
            )),
            _ => Err(JSpaceError::new(
                "JSPACE_UNKNOWN_TOOL",
                "command",
                &command_type,
                "command type is not supported",
            )),
        }
    }

    pub fn check_source_read(&self, target: &Path) -> Result<(), JSpaceError> {
        self.check_operation("command", "source_read")?;
        if !self.source_read {
            if self.directory_source_read {
                return read_commands::check_file(self, target);
            }
            return Err(JSpaceError::new(
                "JSPACE_SOURCE_READ_DENIED",
                "read",
                &target.display().to_string(),
                "source_read is not explicitly granted",
            ));
        }
        if self.mapped_source_read_binding(target).is_some() {
            self.check_operation("read", &target.display().to_string())?;
            self.revalidate_mapped_source_reads()?;
            return Ok(());
        }
        self.check_path("read", target)?;
        let exact = if self.schema_version == JSPACE_SCHEMA_VERSION {
            self.effect_capabilities.iter().any(|capability| {
                capability.allowed_operations.contains("read")
                    && capability
                        .resolve_target(target, "read")
                        .ok()
                        .is_some_and(|relative| capability.read_scopes.matches_exact(&relative))
            })
        } else {
            self.resolve_target(target, "read")
                .ok()
                .is_some_and(|relative| self.read_scopes.matches_exact(&relative))
        };
        if !exact {
            if self.directory_source_read {
                return read_commands::check_file(self, target);
            }
            return Err(JSpaceError::new(
                JSPACE_EXPANSION_REQUIRED,
                "read",
                &target.display().to_string(),
                "source_read requires the exact file path in a read scope",
            ));
        }
        Ok(())
    }

    fn has_declared_patch_target(&self) -> bool {
        let writable = |operations: &HashSet<String>| {
            operations.contains("create") || operations.contains("modify")
        };
        if self.schema_version == JSPACE_SCHEMA_VERSION {
            self.effect_capabilities.iter().any(|capability| {
                writable(&capability.allowed_operations)
                    && capability
                        .declared_projection
                        .iter()
                        .any(|target| capability.write_scopes.matches(target))
            })
        } else {
            writable(&self.allowed_operations)
                && self
                    .declared_target_projection
                    .iter()
                    .any(|target| self.write_scopes.matches(target))
        }
    }

    pub fn ensure_in_root(&self, target: &Path, operation: &str) -> Result<(), JSpaceError> {
        self.resolve_target(target, operation).map(|_| ())
    }

    fn check_effect_path(
        &self,
        operation: &str,
        target: &Path,
        domain_id: Option<&str>,
    ) -> Result<(), JSpaceError> {
        if !target.is_absolute() {
            return Err(JSpaceError::new(
                "JSPACE_TARGET_INVALID",
                operation,
                &target.display().to_string(),
                "multi-domain targets must be exact absolute paths",
            ));
        }
        if self.denied_operations.contains(operation) {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_DENIED",
                operation,
                &target.display().to_string(),
                "operation is explicitly denied",
            ));
        }
        let mut matched = None;
        for capability in &self.effect_capabilities {
            if domain_id.is_some_and(|domain_id| domain_id != capability.domain_id) {
                continue;
            }
            match capability.resolve_target(target, operation) {
                Ok(relative) => {
                    matched = Some((capability, relative));
                    break;
                }
                Err(error) if error.code() == "JSPACE_PATH_OUTSIDE_DOMAIN" => {}
                Err(error) => return Err(error),
            }
        }
        let Some((capability, relative)) = matched else {
            return Err(JSpaceError::new(
                if domain_id.is_some() {
                    "JSPACE_DOMAIN_TARGET_MISMATCH"
                } else {
                    "JSPACE_PATH_OUTSIDE_ROOT"
                },
                operation,
                &target.display().to_string(),
                "target is outside every admitted effect domain",
            ));
        };
        if !capability.allowed_operations.contains(operation) {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_DENIED",
                operation,
                &target.display().to_string(),
                format!(
                    "operation is not admitted in effect domain {}",
                    capability.domain_id
                ),
            ));
        }
        let scope_allowed = if operation == "read" {
            capability.read_scopes.matches(&relative)
        } else {
            capability.write_scopes.matches(&relative)
        };
        if !scope_allowed {
            return Err(JSpaceError::new(
                JSPACE_EXPANSION_REQUIRED,
                operation,
                &target.display().to_string(),
                format!(
                    "target is outside the declared scope for effect domain {}",
                    capability.domain_id
                ),
            ));
        }
        if operation != "read" && !capability.declared_targets.matches(&relative) {
            return Err(JSpaceError::new(
                JSPACE_EXPANSION_REQUIRED,
                operation,
                &target.display().to_string(),
                format!(
                    "mutation target is not exact in effect domain {}",
                    capability.domain_id
                ),
            ));
        }
        Ok(())
    }

    fn check_operation(&self, operation: &str, target: &str) -> Result<(), JSpaceError> {
        if !KNOWN_OPERATIONS.contains(&operation) {
            return Err(JSpaceError::new(
                "JSPACE_UNKNOWN_OPERATION",
                operation,
                target,
                "operation is not in the shared J-Space schema",
            ));
        }
        if self.denied_operations.contains(operation) {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_DENIED",
                operation,
                target,
                self.denied_detail(operation, "operation is explicitly denied"),
            ));
        }
        if !self.allowed_operations.contains(operation) {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_DENIED",
                operation,
                target,
                self.denied_detail(operation, "operation is not explicitly allowed"),
            ));
        }
        Ok(())
    }

    /// Inspect actions are read-and-record: their fenced shell writes task-local files under the
    /// evidence root. Name that route on a refused mutation, so the refusal is not mistaken for a
    /// missing capability (live 2026-10-19: a worker ended its run on a refused apply_patch create
    /// of a script in its own evidence directory).
    fn denied_detail(&self, operation: &str, reason: &str) -> String {
        let evidence_root = self
            .inspect_shell
            .as_ref()
            .and_then(|policy| policy.get("evidence_root"))
            .and_then(Value::as_str);
        match evidence_root {
            Some(root) if matches!(operation, "create" | "modify" | "delete") => format!(
                "{reason}; this inspect action writes and edits task-local files with shell lines \
                 under {root} (admitted, not a switched path); repository paths stay read-only"
            ),
            _ => reason.to_string(),
        }
    }

    fn resolve_target(&self, target: &Path, operation: &str) -> Result<String, JSpaceError> {
        if target.as_os_str().is_empty() {
            return Err(JSpaceError::new(
                "JSPACE_TARGET_INVALID",
                operation,
                "",
                "target must be non-empty",
            ));
        }
        if target
            .components()
            .any(|component| component == Component::ParentDir)
        {
            return Err(JSpaceError::new(
                "JSPACE_PATH_TRAVERSAL",
                operation,
                &target.display().to_string(),
                "parent traversal is not admitted",
            ));
        }
        let lexical = if target.is_absolute() {
            target.to_path_buf()
        } else {
            self.repo_root.join(target)
        };
        let lexical_within_root = lexical.strip_prefix(&self.repo_root).is_ok()
            || (target.is_absolute() && target.strip_prefix(&self.lexical_repo_root).is_ok());
        let resolved = resolve_existing_boundary(&lexical).map_err(|error| {
            JSpaceError::new(
                "JSPACE_PATH_RESOLUTION_FAILED",
                operation,
                &target.display().to_string(),
                error,
            )
        })?;
        if resolved.strip_prefix(&self.repo_root).is_err() {
            let code = if lexical_within_root {
                "JSPACE_SYMLINK_ESCAPE"
            } else {
                "JSPACE_PATH_OUTSIDE_ROOT"
            };
            return Err(JSpaceError::new(
                code,
                operation,
                &target.display().to_string(),
                format!("resolved target is {}", resolved.display()),
            ));
        }
        let relative = resolved
            .strip_prefix(&self.repo_root)
            .map_err(|_| {
                JSpaceError::new(
                    "JSPACE_PATH_OUTSIDE_ROOT",
                    operation,
                    &target.display().to_string(),
                    "target cannot be represented relative to the admitted root",
                )
            })?
            .to_string_lossy()
            .replace('\\', "/");
        Ok(relative)
    }
}

impl EffectCapabilityMatcher {
    fn resolve_target(&self, target: &Path, operation: &str) -> Result<String, JSpaceError> {
        if target
            .components()
            .any(|component| component == Component::ParentDir)
        {
            return Err(JSpaceError::new(
                "JSPACE_PATH_TRAVERSAL",
                operation,
                &target.display().to_string(),
                "parent traversal is not admitted",
            ));
        }
        let lexical_within_root = target.strip_prefix(&self.lexical_root).is_ok()
            || target.strip_prefix(&self.root).is_ok();
        let resolved = resolve_existing_boundary(target).map_err(|error| {
            JSpaceError::new(
                "JSPACE_PATH_RESOLUTION_FAILED",
                operation,
                &target.display().to_string(),
                error,
            )
        })?;
        if resolved.strip_prefix(&self.root).is_err() {
            return Err(JSpaceError::new(
                if lexical_within_root {
                    "JSPACE_SYMLINK_ESCAPE"
                } else {
                    "JSPACE_PATH_OUTSIDE_DOMAIN"
                },
                operation,
                &target.display().to_string(),
                format!(
                    "resolved target is outside effect domain {} at {}",
                    self.domain_id,
                    self.root.display()
                ),
            ));
        }
        Ok(resolved
            .strip_prefix(&self.root)
            .expect("effect root checked")
            .to_string_lossy()
            .replace('\\', "/"))
    }
}

#[derive(Clone)]
struct CachedJSpaceAdmission {
    matcher: Arc<JSpaceMatcher>,
    // Exact canonical bytes, not Value equality (which equates signed zero),
    // bind a cache hit to the complete body actually validated at admission.
    canonical_contract: Arc<str>,
}

#[derive(Clone, Default)]
pub struct JSpaceAdmissionCache {
    entries: Arc<Mutex<HashMap<String, CachedJSpaceAdmission>>>,
    admissions: Arc<AtomicUsize>,
}

impl std::fmt::Debug for JSpaceAdmissionCache {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("JSpaceAdmissionCache")
            .field(
                "entries",
                &self
                    .entries
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .len(),
            )
            .field("admissions", &self.admissions.load(Ordering::SeqCst))
            .finish()
    }
}

impl JSpaceAdmissionCache {
    pub fn admit(
        &self,
        session_id: &str,
        session_root: &Path,
        contract: Option<&Value>,
    ) -> Result<Option<Arc<JSpaceMatcher>>, JSpaceError> {
        let Some(contract) = contract else {
            if self
                .entries
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .contains_key(session_id)
            {
                return Err(JSpaceError::new(
                    "JSPACE_CONTRACT_MISSING_ON_RETRY",
                    "admission",
                    session_id,
                    "a session with a bound capability contract cannot omit it on retry",
                ));
            }
            return Ok(None);
        };
        if session_id.trim().is_empty() {
            return Err(JSpaceError::new(
                "JSPACE_SESSION_ID_MISSING",
                "admission",
                "",
                "a J-Space contract requires a session identity",
            ));
        }
        let normalized_session_root = normalized_root(session_root)?;
        let object = contract.as_object().ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                "",
                "contract must be a JSON object",
            )
        })?;
        let schema_version = required_string(object, "schema_version")?;
        let (claimed_content, claimed_authorization) =
            claimed_contract_digests(object, &schema_version)?;
        let claimed_root = normalized_root(Path::new(&required_string(object, "repo_root")?))?;
        if claimed_root != normalized_session_root {
            return Err(JSpaceError::new(
                "JSPACE_ROOT_MISMATCH",
                "admission",
                &claimed_root.display().to_string(),
                format!("session root is {}", normalized_session_root.display()),
            ));
        }
        let canonical_contract: Arc<str> = Arc::from(canonical_json(contract));
        let cached = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .get(session_id)
            .cloned();
        if let Some(existing) = cached {
            if existing.matcher.repo_root() != normalized_session_root {
                return Err(JSpaceError::new(
                    "JSPACE_ROOT_MISMATCH",
                    "admission",
                    session_id,
                    "existing session root cannot be replaced",
                ));
            }
            if existing.matcher.authorization_semantic_sha256() != claimed_authorization {
                return Err(JSpaceError::new(
                    "JSPACE_CONTRACT_CHANGED",
                    "admission",
                    session_id,
                    "existing session authorization cannot be replaced",
                ));
            }
            if existing.matcher.content_sha256() == claimed_content {
                if existing.canonical_contract != canonical_contract {
                    return Err(JSpaceError::new(
                        "JSPACE_CONTENT_DIGEST_MISMATCH",
                        "admission",
                        session_id,
                        "cached digest strings do not authenticate a changed request body",
                    ));
                }
                return Ok(Some(existing.matcher));
            }
        }
        // Parsing, hashing, filesystem checks and trie compilation do not hold
        // the global cache lock. Unrelated sessions can reuse their admission.
        let matcher = Arc::new(JSpaceMatcher::from_value(
            &normalized_session_root,
            contract,
        )?);
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        // Recheck at publication: a concurrent first admission must not replace
        // another request's root/authorization while this matcher was compiling.
        if let Some(existing) = entries.get(session_id) {
            if existing.matcher.repo_root() != matcher.repo_root() {
                return Err(JSpaceError::new(
                    "JSPACE_ROOT_MISMATCH",
                    "admission",
                    session_id,
                    "concurrent admission bound another root",
                ));
            }
            if existing.matcher.authorization_semantic_sha256()
                != matcher.authorization_semantic_sha256()
            {
                return Err(JSpaceError::new(
                    "JSPACE_CONTRACT_CHANGED",
                    "admission",
                    session_id,
                    "concurrent admission bound another authorization",
                ));
            }
            if existing.canonical_contract == canonical_contract {
                return Ok(Some(Arc::clone(&existing.matcher)));
            }
        } else {
            self.admissions.fetch_add(1, Ordering::SeqCst);
        }
        entries.insert(
            session_id.to_string(),
            CachedJSpaceAdmission {
                matcher: Arc::clone(&matcher),
                canonical_contract,
            },
        );
        Ok(Some(matcher))
    }

    pub fn admissions(&self) -> usize {
        self.admissions.load(Ordering::SeqCst)
    }
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A pinned CPython running a test framework by module (`python -B -m unittest|pytest ...`)
/// executes the framework plus test modules the worker names; their bytes change during the
/// action, so an extra pinned argv file adds no assurance. Such a command may pin no file, which
/// lets a worker run tests it wrote the way a harness loads them (V312 unittest, V313 pytest:
/// 2,935 of 2,993 UTM test files are pytest-style). Every other verifier still pins 1..8 files.
fn module_test_runner(argv: &[String]) -> bool {
    let name = Path::new(&argv[0]).file_name().and_then(|part| part.to_str()).unwrap_or_default();
    let cpython = name.strip_prefix("python").is_some_and(|version| {
        !version.is_empty() && version.bytes().all(|byte| byte.is_ascii_digit() || byte == b'.')
    });
    cpython
        && argv.iter().skip(1).position(|arg| arg == "-m").is_some_and(|offset| {
            argv[1..=offset].iter().all(|flag| flag == "-B")
                && argv.get(offset + 2).is_some_and(|module| matches!(module.as_str(), "unittest" | "pytest"))
        })
}

fn verifier_error(code: &str, path: &Path, detail: &str) -> JSpaceError {
    JSpaceError::new(code, "admission", &path.display().to_string(), detail)
}

/// Characters only a shell gives meaning: separators, redirection, substitution and quoting.
/// Verifier argv is exec'd directly, so any of these means the author wrote shell syntax.
fn shell_syntax(ch: char) -> bool {
    matches!(ch, ';' | '&' | '|' | '<' | '>' | '$' | '`' | '\\' | '"' | '\'' | '\r' | '\n' | '\0')
}

/// Glob, brace and grouping characters. Without a shell nothing expands them, so in an operand
/// they are literal data the program reads itself (`unittest -p test_*.py`, a pytest node ID
/// `test_x.py::t[a]`, `pytest -k "(a or b)"`). Only the program path must be free of them.
fn glob_syntax(ch: char) -> bool {
    matches!(ch, '*' | '?' | '[' | ']' | '{' | '}' | '(' | ')')
}

fn cpython_name(name: &str) -> bool {
    name.strip_prefix("python")
        .is_some_and(|version| version.bytes().all(|byte| byte.is_ascii_digit() || byte == b'.'))
}

/// CPython runs inline code only for a `-c` among its own options, which end at `-m` (the module
/// owns every later operand), `--`, `-` or the script. `python -m pytest -c pytest.ini` names
/// pytest's configuration file, not code.
fn cpython_inline_code(argv: &[String]) -> bool {
    let mut args = argv.iter().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--check-hash-based-pycs" {
            args.next();
            continue;
        }
        if arg == "--" || arg == "-" || !arg.starts_with('-') {
            return false;
        }
        if arg.starts_with("--") {
            continue;
        }
        let flags = &arg[1..];
        for (offset, flag) in flags.char_indices() {
            match flag {
                'c' => return true,
                'm' => return false,
                'W' | 'X' => {
                    // The option value is the rest of this token, or the next token.
                    if offset + flag.len_utf8() == flags.len() {
                        args.next();
                    }
                    break;
                }
                _ => {}
            }
        }
    }
    false
}

/// An operand this action writes is its work product: its bytes change by design, so it cannot
/// be pinned. A verifier of work products, with no existing unwritten file operand inside or
/// outside the workspace, has nothing to pin; its program is the pinned executable itself.
fn work_product_operands_only(argv: &[String], workspace: &Path, write_scopes: &[String]) -> bool {
    let written = |path: &Path| {
        path.strip_prefix(workspace).ok().and_then(Path::to_str).is_some_and(|relative| {
            write_scopes.iter().any(|scope| {
                scope == relative
                    || scope
                        .strip_suffix("/**")
                        .is_some_and(|base| relative.strip_prefix(base).is_some_and(|rest| rest.starts_with('/')))
            })
        })
    };
    let operands: Vec<&Path> = argv.iter().skip(1).map(Path::new).filter(|path| path.is_absolute()).collect();
    operands.iter().any(|path| written(path)) && !operands.iter().any(|path| path.is_file() && !written(path))
}

fn verifier_path_without_symlink(path: &Path, must_exist: bool) -> Result<(), JSpaceError> {
    let raw = path.to_str().unwrap_or_default();
    if !path.is_absolute()
        || raw.contains('\0')
        || raw
            .split('/')
            .skip(1)
            .any(|part| part.is_empty() || part == "." || part == "..")
        || path
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(verifier_error(
            "JSPACE_VERIFIER_PATH_INVALID",
            path,
            "path must be canonical absolute syntax",
        ));
    }
    let mut prefix = PathBuf::new();
    let mut missing = false;
    for component in path.components() {
        prefix.push(component.as_os_str());
        if missing {
            continue;
        }
        match std::fs::symlink_metadata(&prefix) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_SYMLINK_DENIED",
                    &prefix,
                    "symlink component is not admitted",
                ));
            }
            Ok(metadata) if prefix.as_path() != path && !metadata.is_dir() => {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_PATH_INVALID",
                    &prefix,
                    "path ancestor must be a directory",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && !must_exist => {
                missing = true
            }
            Err(_) => {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_PATH_INVALID",
                    &prefix,
                    "path unavailable",
                ));
            }
        }
    }
    Ok(())
}

fn verifier_python_import_roots(
    command: &VerifierCommand,
    _matcher: &JSpaceMatcher,
) -> Result<(), JSpaceError> {
    let Some(roots) = &command.python_import_roots else {
        return Ok(());
    };
    const CODE: &str = "JSPACE_VERIFIER_PYTHON_IMPORT_ROOTS_INVALID";
    let executable = Path::new(&command.argv[0]);
    let name = executable.file_name().and_then(|part| part.to_str()).unwrap_or_default();
    let cpython_name = name.strip_prefix("python").is_some_and(|suffix| {
        suffix.bytes().all(|byte| byte.is_ascii_digit() || byte == b'.')
    });
    let ignores_environment = command.argv[1..].iter().any(|arg| {
        arg.starts_with('-') && !arg.starts_with("--")
            // Attached -W/-X values and -m/-c entrypoints are not flag clusters.
            && arg[1..].bytes()
                .take_while(|byte| !matches!(*byte, b'W' | b'X' | b'm' | b'c'))
                .any(|byte| matches!(byte, b'I' | b'E'))
    });
    if !(1..=4).contains(&roots.len()) || !cpython_name || ignores_environment {
        return Err(verifier_error(
            CODE, executable,
            "1..4 import roots require a CPython-named executable without -I/-E",
        ));
    }
    // 2026-10-22 (Nokiy V312): focused verifiers read globally while writes stay confined to the
    // scratch root and network stays denied, so an import root is search-path context, never a
    // grant. A worker may then run tests the way a harness loads them: from the workspace root,
    // a frozen acceptance directory outside the workspace or a virtualenv site-packages. Only
    // canonical existing directories outside Git metadata qualify.
    let mut seen = HashSet::new();
    for root in roots {
        let raw = root.to_str().unwrap_or_default();
        if raw.is_empty() || raw.contains(':') || raw.chars().count() > 1024
            || !root.is_absolute()
            || !seen.insert(root.clone())
            || root.components().any(|part| part.as_os_str() == ".git")
        {
            return Err(verifier_error(
                CODE, root, "distinct bounded absolute directories outside Git metadata required",
            ));
        }
        verifier_path_without_symlink(root, true)?;
        if !root.is_dir() || root.canonicalize().ok().as_deref() != Some(root.as_path()) {
            return Err(verifier_error(CODE, root, "canonical existing directory required"));
        }
    }
    Ok(())
}

fn verifier_cwd(command: &VerifierCommand) -> Result<(), JSpaceError> {
    let Some(cwd) = &command.cwd else {
        return Ok(());
    };
    // 2026-10-22 (Nokiy V318): a focused verifier reads globally and writes only its scratch
    // root, so its starting directory grants nothing. It lets a worker run its tests from the
    // directory its acceptance harness uses (I-14: tests that found a helper through the working
    // directory passed from the worker's workspace and failed only when run from task_b).
    const CODE: &str = "JSPACE_VERIFIER_CWD_INVALID";
    let raw = cwd.to_str().unwrap_or_default();
    if raw.is_empty()
        || raw.contains(':')
        || raw.chars().count() > 1024
        || !cwd.is_absolute()
        || cwd.components().any(|part| part.as_os_str() == ".git")
    {
        return Err(verifier_error(
            CODE, cwd, "bounded absolute directory outside Git metadata required",
        ));
    }
    verifier_path_without_symlink(cwd, true)?;
    if !cwd.is_dir() || cwd.canonicalize().ok().as_deref() != Some(cwd.as_path()) {
        return Err(verifier_error(CODE, cwd, "canonical existing directory required"));
    }
    Ok(())
}

fn verifier_node_path_is_sensitive(path: &Path) -> bool {
    path.ancestors().any(|ancestor| {
        matches!(ancestor.file_name().and_then(|name| name.to_str()),
            Some(".git" | ".ssh" | ".gnupg" | ".aws" | ".docker" | ".netrc"))
            || [".config/gh", "Library/Keychains", ".codex/auth.json"].iter()
                .any(|suffix| ancestor.ends_with(suffix))
    })
}

/// Recognize argv shape only; callers must still validate the executable, scope and file pins.
/// Node options before the program and indirect entrypoints never qualify.
pub fn direct_node_verifier_program(argv: &[String]) -> Option<&Path> {
    let executable = Path::new(argv.first()?);
    let program_arg = argv.get(1)?;
    let program = Path::new(program_arg);
    (executable.is_absolute()
        && executable.file_name().and_then(|name| name.to_str()) == Some("node")
        && !verifier_node_path_is_sensitive(executable)
        && program.is_absolute()
        && !verifier_node_path_is_sensitive(program)
        && matches!(program.extension().and_then(|ext| ext.to_str()), Some("js" | "cjs" | "mjs"))
        && argv.iter().filter(|arg| *arg == program_arg).count() == 1)
        .then_some(program)
}

/// CPython and Node open their entry operands as program or test source and treat a leading
/// `#!` line as a comment, never dispatching on it, so such a header is data. Only an operand
/// another program may exec reaches the kernel's `#!` dispatch.
pub fn verifier_interpreter_reads_entries(argv: &[String]) -> bool {
    let name = argv
        .first()
        .and_then(|arg| Path::new(arg).file_name())
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    name == "node"
        || name
            .strip_prefix("python")
            .is_some_and(|rest| rest.chars().all(|ch| ch.is_ascii_digit() || ch == '.'))
}

fn verifier_file(
    path: &Path,
    expected: &str,
    executable: bool,
    stale: bool,
    header_is_data: bool,
) -> Result<(), JSpaceError> {
    use std::os::unix::fs::PermissionsExt;
    verifier_path_without_symlink(path, true)?;
    if !is_lower_sha256(expected) {
        return Err(verifier_error(
            "JSPACE_VERIFIER_GRANT_MALFORMED",
            path,
            "lowercase SHA-256 required",
        ));
    }
    let metadata = std::fs::metadata(path).map_err(|_| {
        verifier_error(
            "JSPACE_VERIFIER_PATH_INVALID",
            path,
            "verifier file unavailable",
        )
    })?;
    if !metadata.is_file() || (executable && metadata.permissions().mode() & 0o111 == 0) {
        return Err(verifier_error(
            "JSPACE_VERIFIER_GRANT_MALFORMED",
            path,
            "regular executable file required",
        ));
    }
    let mut file = std::fs::File::open(path).map_err(|_| {
        verifier_error(
            "JSPACE_VERIFIER_PATH_INVALID",
            path,
            "verifier file unavailable",
        )
    })?;
    let mut header = [0u8; 4];
    if executable {
        file.read_exact(&mut header).map_err(|_| {
            verifier_error(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                path,
                "pinned Mach-O executable required",
            )
        })?;
        if !matches!(
            header,
            [0xfe, 0xed, 0xfa, 0xce]
                | [0xce, 0xfa, 0xed, 0xfe]
                | [0xfe, 0xed, 0xfa, 0xcf]
                | [0xcf, 0xfa, 0xed, 0xfe]
                | [0xca, 0xfe, 0xba, 0xbe]
                | [0xbe, 0xba, 0xfe, 0xca]
                | [0xca, 0xfe, 0xba, 0xbf]
                | [0xbf, 0xba, 0xfe, 0xca]
        ) {
            return Err(verifier_error(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                path,
                "pinned Mach-O executable required",
            ));
        }
        use std::io::Seek;
        file.rewind().map_err(|_| {
            verifier_error(
                "JSPACE_VERIFIER_PATH_INVALID",
                path,
                "verifier file unavailable",
            )
        })?;
    }
    let mut shebang = [0u8; 256];
    let count = file.read(&mut shebang).map_err(|_| {
        verifier_error(
            "JSPACE_VERIFIER_PATH_INVALID",
            path,
            "verifier file unavailable",
        )
    })?;
    if !header_is_data && shebang[..count].starts_with(b"#!") {
        let line = shebang[2..count]
            .split(|byte| *byte == b'\n')
            .next()
            .unwrap_or_default();
        let ascii = line
            .iter()
            .copied()
            .filter(u8::is_ascii)
            .collect::<Vec<_>>();
        let text = String::from_utf8_lossy(&ascii);
        let first = text
            .split_whitespace()
            .next()
            .unwrap_or_default()
            .rsplit('/')
            .next()
            .unwrap_or_default();
        // An operand another program may exec: the kernel would run this header, and env
        // would resolve its program from PATH, outside every pin.
        if matches!(
            first,
            "sh" | "bash" | "dash" | "zsh" | "fish" | "csh" | "tcsh" | "ksh" | "env" | "osascript"
        ) {
            return Err(verifier_error(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                path,
                "shell and env verifier entries are not admitted",
            ));
        }
    }
    use std::io::Seek;
    file.rewind().map_err(|_| {
        verifier_error(
            "JSPACE_VERIFIER_PATH_INVALID",
            path,
            "verifier file unavailable",
        )
    })?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 8192];
    loop {
        let count = file.read(&mut buffer).map_err(|_| {
            verifier_error(
                "JSPACE_VERIFIER_PATH_INVALID",
                path,
                "verifier file unavailable",
            )
        })?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    if format!("{:x}", hasher.finalize()) != expected {
        return Err(verifier_error(
            if stale {
                "NOKIY_LOCAL_CONTEXT_STALE"
            } else {
                "JSPACE_VERIFIER_FILE_MISMATCH"
            },
            path,
            "verifier file SHA-256 changed",
        ));
    }
    Ok(())
}

fn verifier_scratch(
    path: &Path,
    root: &Path,
    workspace: &Path,
    must_exist: bool,
) -> Result<(), JSpaceError> {
    verifier_path_without_symlink(path, must_exist)?;
    if path == root || !path.starts_with(root) || path.starts_with(workspace) {
        return Err(verifier_error(
            "JSPACE_VERIFIER_SCRATCH_INVALID",
            path,
            "scratch must be strictly below artifact root and outside workspace",
        ));
    }
    let mut current = path;
    loop {
        if current == root {
            break;
        }
        match std::fs::symlink_metadata(current) {
            Ok(metadata) if !metadata.is_dir() => {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_SCRATCH_INVALID",
                    current,
                    "scratch components must be directories",
                ));
            }
            Err(error) if !must_exist && error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_SCRATCH_NOT_READY",
                    current,
                    "scratch directory unavailable",
                ));
            }
            _ => {}
        }
        current = current.parent().ok_or_else(|| {
            verifier_error(
                "JSPACE_VERIFIER_SCRATCH_INVALID",
                path,
                "invalid scratch parent",
            )
        })?;
    }
    if must_exist && path.canonicalize().ok().as_deref() != Some(path) {
        return Err(verifier_error(
            "JSPACE_VERIFIER_SCRATCH_NOT_READY",
            path,
            "scratch identity changed",
        ));
    }
    Ok(())
}

fn parse_verifier_grant(
    object: &Map<String, Value>,
    workspace: &Path,
    schema: &str,
) -> Result<(Option<PathBuf>, Vec<VerifierCommand>), JSpaceError> {
    if !object.contains_key("verifier_commands") && !object.contains_key("verifier_artifact_root") {
        return Ok((None, Vec::new()));
    }
    if schema != JSPACE_SINGLE_ROOT_SCHEMA_VERSION {
        return Err(JSpaceError::new(
            "JSPACE_VERIFIER_GRANT_UNSUPPORTED",
            "admission",
            "verifier_commands",
            "typed verifier fields require single-root v2",
        ));
    }
    let raw_commands = object.get("verifier_commands").ok_or_else(|| {
        JSpaceError::new(
            "JSPACE_VERIFIER_GRANT_MALFORMED",
            "admission",
            "verifier_commands",
            "paired fields required",
        )
    })?;
    let commands: Vec<VerifierCommand> =
        serde_json::from_value(raw_commands.clone()).map_err(|error| {
            JSpaceError::new(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                "admission",
                "verifier_commands",
                error.to_string(),
            )
        })?;
    if !(1..=8).contains(&commands.len()) {
        return Err(JSpaceError::new(
            "JSPACE_VERIFIER_GRANT_MALFORMED",
            "admission",
            "verifier_commands",
            "1..8 commands required",
        ));
    }
    let root = object
        .get("verifier_artifact_root")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                "admission",
                "verifier_artifact_root",
                "paired absolute root required",
            )
        })?;
    verifier_path_without_symlink(&root, true)?;
    if !root.is_dir()
        || root.canonicalize().ok().as_deref() != Some(root.as_path())
        || root.starts_with(workspace)
    {
        return Err(verifier_error(
            "JSPACE_VERIFIER_ARTIFACT_ROOT_INVALID",
            &root,
            "canonical artifact directory must be outside workspace",
        ));
    }
    let mut argv_seen = HashSet::new();
    let mut scratches = HashSet::new();
    let write_scopes: Vec<String> = object
        .get("write_scopes")
        .and_then(Value::as_array)
        .map(|scopes| scopes.iter().filter_map(Value::as_str).map(str::to_owned).collect())
        .unwrap_or_default();
    for command in &commands {
        let unpinned = !command.argv.is_empty()
            && (module_test_runner(&command.argv)
                || work_product_operands_only(&command.argv, workspace, &write_scopes));
        if !(1..=32).contains(&command.argv.len())
            || !(if unpinned { 0..=8 } else { 1..=8 })
                .contains(&command.pinned_files.len())
            || !(1..=300).contains(&command.timeout_seconds)
            || command.network
            || !argv_seen.insert(command.argv.clone())
        {
            return Err(JSpaceError::new(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                "admission",
                "verifier_commands",
                "invalid bounds, network policy or duplicate argv",
            ));
        }
        if command.argv.iter().enumerate().any(|(index, arg)| {
            arg.is_empty()
                || arg.chars().count() > 1024
                || arg.chars().any(|ch| shell_syntax(ch) || (index == 0 && glob_syntax(ch)))
        }) {
            return Err(JSpaceError::new(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                "admission",
                "verifier_commands",
                "argv must be bounded and shell-free",
            ));
        }
        let executable = Path::new(&command.argv[0]);
        let name = executable
            .file_name()
            .and_then(|part| part.to_str())
            .unwrap_or_default();
        if matches!(
            name,
            "sh" | "bash" | "dash" | "zsh" | "fish" | "csh" | "tcsh" | "ksh" | "env" | "osascript"
        ) || (if cpython_name(name) {
            cpython_inline_code(&command.argv)
        } else {
            ["python", "node", "ruby", "perl", "php"]
                .iter()
                .any(|prefix| name.starts_with(prefix))
                && command.argv[1..]
                    .iter()
                    .any(|arg| matches!(arg.as_str(), "-c" | "-e" | "--eval" | "--execute"))
        })
        {
            return Err(JSpaceError::new(
                "JSPACE_VERIFIER_GRANT_MALFORMED",
                "admission",
                "verifier_commands",
                "shell or inline interpreter invocation denied",
            ));
        }
        verifier_file(executable, &command.executable_sha256, true, false, false)?;
        verifier_scratch(&command.scratch_root, &root, workspace, false)?;
        if !scratches.insert(command.scratch_root.clone()) {
            return Err(verifier_error(
                "JSPACE_VERIFIER_SCRATCH_INVALID",
                &command.scratch_root,
                "duplicate scratch root",
            ));
        }
        let mut pinned = HashSet::new();
        for file in &command.pinned_files {
            let occurrences = command.argv.iter()
                .filter(|arg| file.path.to_str() == Some(arg.as_str())).count();
            if !is_lower_sha256(&file.sha256)
                || !pinned.insert(file.path.clone())
                || occurrences == 0
                || (file.scope.is_some() && occurrences != 1)
            {
                return Err(verifier_error(
                    "JSPACE_VERIFIER_GRANT_MALFORMED",
                    &file.path,
                    "pinned file must have a valid digest and occur in exact argv; verifier_only requires exactly once",
                ));
            }
        }
    }
    Ok((Some(root), commands))
}

fn claimed_contract_digests(
    object: &Map<String, Value>,
    schema_version: &str,
) -> Result<(String, String), JSpaceError> {
    let (content, authorization) = match schema_version {
        JSPACE_SCHEMA_VERSION | JSPACE_SINGLE_ROOT_SCHEMA_VERSION => (
            required_string(object, "content_sha256")?,
            required_string(object, "authorization_semantic_sha256")?,
        ),
        JSPACE_LEGACY_SCHEMA_VERSION => {
            let digest = required_string(object, "semantic_sha256")?;
            (digest.clone(), digest)
        }
        _ => {
            return Err(JSpaceError::new(
                "JSPACE_SCHEMA_VERSION_UNSUPPORTED",
                "admission",
                "schema_version",
                format!("unsupported schema {schema_version}"),
            ));
        }
    };
    if !is_lower_sha256(&content) || !is_lower_sha256(&authorization) {
        return Err(JSpaceError::new(
            "JSPACE_DIGEST_INVALID",
            "admission",
            "",
            "J-Space digests must be lowercase SHA-256",
        ));
    }
    Ok((content, authorization))
}

fn verified_contract_digests(
    contract: &Value,
    object: &Map<String, Value>,
    schema_version: &str,
) -> Result<(String, String), JSpaceError> {
    let (content, authorization) = claimed_contract_digests(object, schema_version)?;
    if schema_version == JSPACE_SCHEMA_VERSION
        || schema_version == JSPACE_SINGLE_ROOT_SCHEMA_VERSION
    {
        let expected_authorization = authorization_semantic_sha256(contract)?;
        if authorization != expected_authorization {
            return Err(JSpaceError::new(
                "JSPACE_AUTHORIZATION_DIGEST_MISMATCH",
                "admission",
                "",
                format!("expected {expected_authorization}, got {authorization}"),
            ));
        }
        let mut payload = contract.clone();
        payload
            .as_object_mut()
            .ok_or_else(|| {
                JSpaceError::new(
                    "JSPACE_CONTRACT_MALFORMED",
                    "admission",
                    "",
                    "contract must be an object",
                )
            })?
            .remove("content_sha256");
        let expected_content = semantic_sha256(&payload);
        if content != expected_content {
            return Err(JSpaceError::new(
                "JSPACE_CONTENT_DIGEST_MISMATCH",
                "admission",
                "",
                format!("expected {expected_content}, got {content}"),
            ));
        }
    } else {
        let mut payload = contract.clone();
        payload
            .as_object_mut()
            .ok_or_else(|| {
                JSpaceError::new(
                    "JSPACE_CONTRACT_MALFORMED",
                    "admission",
                    "",
                    "contract must be an object",
                )
            })?
            .remove("semantic_sha256");
        let expected = semantic_sha256(&payload);
        if content != expected {
            return Err(JSpaceError::new(
                "JSPACE_SEMANTIC_DIGEST_MISMATCH",
                "admission",
                "",
                format!("expected {expected}, got {content}"),
            ));
        }
    }
    Ok((content, authorization))
}

pub fn authorization_semantic_sha256(contract: &Value) -> Result<String, JSpaceError> {
    let object = contract.as_object().ok_or_else(|| {
        JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            "",
            "contract must be an object",
        )
    })?;
    let mapped_source_reads = parse_mapped_source_read_grant(object)?;
    if (object.contains_key("verifier_commands") || object.contains_key("verifier_artifact_root"))
        && object.get("schema_version").and_then(Value::as_str)
            != Some(JSPACE_SINGLE_ROOT_SCHEMA_VERSION)
    {
        return Err(JSpaceError::new(
            "JSPACE_VERIFIER_GRANT_UNSUPPORTED",
            "admission",
            "verifier_commands",
            "typed verifier fields require single-root v2",
        ));
    }
    if object.get("schema_version").and_then(Value::as_str) == Some(JSPACE_SCHEMA_VERSION) {
        return normalized_effect_authorization_sha256(object);
    }
    let source_read = source_read_grant(object)?;
    let generation = object
        .get("dcf_generation")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                "dcf_generation",
                "required object is missing",
            )
        })?;
    let mut payload = serde_json::json!({
        "schema_version": JSPACE_AUTHORIZATION_SCHEMA_VERSION,
        "repo_root": object.get("repo_root").cloned().unwrap_or(Value::Null),
        "required_domain_bindings": generation
            .get("required_domain_bindings")
            .cloned()
            .unwrap_or(Value::Null),
        "matched_surface_ids": object
            .get("matched_surface_ids")
            .cloned()
            .unwrap_or(Value::Null),
        "read_scopes": object.get("read_scopes").cloned().unwrap_or(Value::Null),
        "write_scopes": object.get("write_scopes").cloned().unwrap_or(Value::Null),
        "allowed_operations": object
            .get("allowed_operations")
            .cloned()
            .unwrap_or(Value::Null),
        "denied_operations": object
            .get("denied_operations")
            .cloned()
            .unwrap_or(Value::Null),
        "command_templates": object
            .get("command_templates")
            .cloned()
            .unwrap_or(Value::Null),
        "declared_targets": object
            .get("declared_targets")
            .cloned()
            .unwrap_or(Value::Null),
        "expansion": object.get("expansion").cloned().unwrap_or(Value::Null),
    });
    if let Some(policy) = object.get("command_effect_policy") {
        if policy.as_str() != Some("trusted_argv_effects_v1") {
            return Err(JSpaceError::new(
                "JSPACE_COMMAND_EFFECT_POLICY_UNSUPPORTED",
                "admission",
                "command_effect_policy",
                "unsupported command effect policy",
            ));
        }
        payload["command_effect_policy"] = policy.clone();
    }
    if let Some(policy) = object.get("read_commands") {
        read_commands::validate(policy)?;
        payload["read_commands"] = policy.clone();
    }
    if let Some(policy) = object.get("inspect_shell") {
        inspect_shell::validate(policy)?;
        payload["inspect_shell"] = policy.clone();
    }
    if let Some(policy) = object.get("parent_shell") {
        parent_shell::validate(policy)?;
        payload["parent_shell"] = policy.clone();
    }
    if let Some(commands) = object.get("verifier_commands") {
        payload["verifier_commands"] = commands.clone();
        payload["verifier_artifact_root"] = object
            .get("verifier_artifact_root")
            .cloned()
            .unwrap_or(Value::Null);
    } else if object.contains_key("verifier_artifact_root") {
        payload["verifier_artifact_root"] = object
            .get("verifier_artifact_root")
            .cloned()
            .unwrap_or(Value::Null);
    }
    if source_read {
        payload["source_read"] = Value::Bool(true);
    }
    if !mapped_source_reads.is_empty() {
        payload["mapped_source_reads"] = serde_json::to_value(&mapped_source_reads).map_err(|error| {
            JSpaceError::new(
                "JSPACE_MAPPED_SOURCE_READ_GRANT_INVALID",
                "admission",
                "mapped_source_reads",
                error.to_string(),
            )
        })?;
    }
    Ok(semantic_sha256(&payload))
}

fn parse_mapped_source_read_grant(
    object: &Map<String, Value>,
) -> Result<Vec<MappedSourceReadBinding>, JSpaceError> {
    let Some(value) = object.get("mapped_source_reads") else {
        return Ok(Vec::new());
    };
    let invalid = |detail: String| {
        JSpaceError::new(
            "JSPACE_MAPPED_SOURCE_READ_GRANT_INVALID",
            "admission",
            "mapped_source_reads",
            detail,
        )
    };
    if object.get("schema_version").and_then(Value::as_str)
        != Some(JSPACE_SINGLE_ROOT_SCHEMA_VERSION)
        || object.get("source_read").and_then(Value::as_bool) != Some(true)
    {
        return Err(invalid("mapped reads require source_read=true on single-root v2".into()));
    }
    let bindings: Vec<MappedSourceReadBinding> = serde_json::from_value(value.clone())
        .map_err(|error| invalid(error.to_string()))?;
    if bindings.len() != 1 {
        return Err(invalid("exactly one mapped source read binding is required".into()));
    }
    let binding = &bindings[0];
    let logical = &binding.logical_path;
    if binding.requested_logical_path != *logical
        || logical.is_empty()
        || logical.len() > 64 * 1024
        || logical.contains('\\')
        || logical.chars().any(|character| {
            character.is_control() || matches!(character, '*' | '?' | '[' | ']')
        })
        || logical.split('/').any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(invalid("the mapped request must be the canonical relative logical path".into()));
    }
    let allowed = operation_set(
        required_string_array(object, "allowed_operations")?,
        "allowed_operations",
    )?;
    let denied = operation_set(
        required_string_array(object, "denied_operations")?,
        "denied_operations",
    )?;
    if !allowed.contains("read")
        || !allowed.contains("command")
        || denied.contains("read")
        || denied.contains("command")
        || !required_string_array(object, "read_scopes")?.contains(logical)
    {
        return Err(invalid("mapped reads require existing read/command grants and an exact logical read scope".into()));
    }
    Ok(bindings)
}

fn source_read_grant(object: &Map<String, Value>) -> Result<bool, JSpaceError> {
    match object.get("source_read") {
        None => Ok(false),
        Some(Value::Bool(true))
            if matches!(
                object.get("schema_version").and_then(Value::as_str),
                Some(JSPACE_SCHEMA_VERSION | JSPACE_SINGLE_ROOT_SCHEMA_VERSION)
            ) =>
        {
            Ok(true)
        }
        _ => Err(JSpaceError::new(
            "JSPACE_SOURCE_READ_GRANT_INVALID",
            "admission",
            "source_read",
            "source_read must be true on a v2 or v3 contract",
        )),
    }
}

fn normalized_effect_authorization_sha256(
    object: &Map<String, Value>,
) -> Result<String, JSpaceError> {
    let source_read = source_read_grant(object)?;
    let generation = object
        .get("dcf_generation")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                "dcf_generation",
                "required object is missing",
            )
        })?;
    let allowed_operations = operation_set(
        required_string_array(object, "allowed_operations")?,
        "allowed_operations",
    )?;
    let denied_operations = operation_set(
        required_string_array(object, "denied_operations")?,
        "denied_operations",
    )?;
    let capabilities = parse_effect_capabilities(object, &denied_operations)?;
    let templates = parse_command_templates(object, true)?;
    validate_command_templates(
        &templates,
        &allowed_operations,
        &denied_operations,
        &PathTrie::new(),
        &capabilities,
        true,
    )?;

    let mut capability_values = capabilities
        .iter()
        .map(|capability| {
            let mut operations = capability
                .allowed_operations
                .iter()
                .cloned()
                .collect::<Vec<_>>();
            operations.sort();
            serde_json::json!({
                "domain_id": capability.domain_id,
                "root": capability.root,
                "read_scopes": capability.read_projection,
                "write_scopes": capability.write_projection,
                "allowed_operations": operations,
                "declared_targets": capability.declared_projection,
            })
        })
        .collect::<Vec<_>>();
    capability_values.sort_by_key(canonical_json);

    let mut template_values = templates
        .iter()
        .map(|template| {
            let mut effects = template.effects.clone();
            effects.sort();
            effects.dedup();
            let mut targets = template
                .targets
                .iter()
                .map(|target| {
                    serde_json::json!({
                        "domain_id": target.domain_id,
                        "operation": target.operation,
                        "path": resolve_existing_boundary(Path::new(&target.path))
                            .map(|path| path.display().to_string())
                            .unwrap_or_else(|_| target.path.clone()),
                        "argv_index": target.argv_index,
                        "binding": "argv",
                    })
                })
                .collect::<Vec<_>>();
            targets.sort_by_key(canonical_json);
            serde_json::json!({
                "argv": template.argv,
                "effects": effects,
                "targets": targets,
            })
        })
        .collect::<Vec<_>>();
    template_values.sort_by_key(canonical_json);

    let mut matched_surface_ids = required_string_array(object, "matched_surface_ids")?;
    matched_surface_ids.sort();
    matched_surface_ids.dedup();
    let mut global_allowed = allowed_operations.into_iter().collect::<Vec<_>>();
    global_allowed.sort();
    let mut global_denied = denied_operations.into_iter().collect::<Vec<_>>();
    global_denied.sort();
    let mut payload = serde_json::json!({
        "schema_version": JSPACE_EFFECT_AUTHORIZATION_SCHEMA_VERSION,
        "repo_root": normalized_root(Path::new(&required_string(object, "repo_root")?))?,
        "required_domain_bindings": generation
            .get("required_domain_bindings")
            .cloned()
            .unwrap_or(Value::Null),
        "matched_surface_ids": matched_surface_ids,
        "allowed_operations": global_allowed,
        "denied_operations": global_denied,
        "effect_capabilities": capability_values,
        "command_templates": template_values,
        "expansion": object.get("expansion").cloned().unwrap_or(Value::Null),
    });
    if source_read {
        payload["source_read"] = Value::Bool(true);
    }
    Ok(semantic_sha256(&payload))
}

fn parse_effect_capabilities(
    object: &Map<String, Value>,
    denied_operations: &HashSet<String>,
) -> Result<Vec<EffectCapabilityMatcher>, JSpaceError> {
    let Some(Value::Array(values)) = object.get("effect_capabilities") else {
        return Err(JSpaceError::new(
            "JSPACE_EFFECT_CAPABILITIES_MISSING",
            "admission",
            "effect_capabilities",
            "multi-domain contracts require an explicit capability array",
        ));
    };
    if values.is_empty() {
        return Err(JSpaceError::new(
            "JSPACE_EFFECT_CAPABILITIES_MISSING",
            "admission",
            "effect_capabilities",
            "at least one bounded effect domain is required",
        ));
    }
    let mut domain_ids = HashSet::new();
    let mut capabilities = Vec::new();
    for value in values {
        let capability = value.as_object().ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                "effect_capabilities",
                "capability member is not an object",
            )
        })?;
        let domain_id = required_string(capability, "domain_id")?;
        if domain_id.trim() != domain_id || !domain_ids.insert(domain_id.clone()) {
            return Err(JSpaceError::new(
                "JSPACE_DOMAIN_ID_INVALID",
                "admission",
                &domain_id,
                "effect domain identity must be unique and whitespace-normalized",
            ));
        }
        let lexical_root = PathBuf::from(required_string(capability, "root")?);
        if !lexical_root.is_absolute() {
            return Err(JSpaceError::new(
                "JSPACE_DOMAIN_ROOT_INVALID",
                "admission",
                &lexical_root.display().to_string(),
                "effect domain root must be absolute",
            ));
        }
        if has_symlink_component(&lexical_root)? {
            return Err(JSpaceError::new(
                "JSPACE_DOMAIN_ROOT_SYMLINK",
                "admission",
                &lexical_root.display().to_string(),
                "effect domain roots must not contain symlink aliases",
            ));
        }
        let root = normalized_root(&lexical_root)?;
        if root.parent().is_none() {
            return Err(JSpaceError::new(
                "JSPACE_DOMAIN_ROOT_TOO_BROAD",
                "admission",
                &root.display().to_string(),
                "filesystem root cannot be admitted as an effect domain",
            ));
        }
        let read_projection =
            canonical_scope_set(required_string_array(capability, "read_scopes")?)?;
        let write_projection =
            canonical_scope_set(required_string_array(capability, "write_scopes")?)?;
        let declared_projection =
            canonical_declared_targets(&required_string_array(capability, "declared_targets")?)?;
        let allowed_operations = operation_set(
            required_string_array(capability, "allowed_operations")?,
            "effect_capabilities.allowed_operations",
        )?;
        if allowed_operations.is_empty()
            || allowed_operations.iter().any(|operation| {
                !matches!(operation.as_str(), "read" | "create" | "modify" | "delete")
                    || denied_operations.contains(operation)
            })
        {
            return Err(JSpaceError::new(
                "JSPACE_CAPABILITY_PERMISSION_INVALID",
                "admission",
                &domain_id,
                "effect domain permissions must be non-empty admitted path operations",
            ));
        }
        let mut read_scopes = PathTrie::new();
        for scope in &read_projection {
            read_scopes.insert(scope)?;
        }
        let mut write_scopes = PathTrie::new();
        for scope in &write_projection {
            write_scopes.insert(scope)?;
        }
        let mut declared_targets = PathTrie::new();
        for target in &declared_projection {
            declared_targets.insert(target)?;
        }
        capabilities.push(EffectCapabilityMatcher {
            domain_id,
            root,
            lexical_root,
            read_projection,
            write_projection,
            declared_projection,
            read_scopes,
            write_scopes,
            declared_targets,
            allowed_operations,
        });
    }
    capabilities.sort_by(|left, right| left.domain_id.cmp(&right.domain_id));
    for (index, left) in capabilities.iter().enumerate() {
        for right in capabilities.iter().skip(index + 1) {
            if left.root.starts_with(&right.root) || right.root.starts_with(&left.root) {
                return Err(JSpaceError::new(
                    "JSPACE_DOMAIN_ROOT_OVERLAP",
                    "admission",
                    &left.root.display().to_string(),
                    format!(
                        "effect domains {} and {} overlap or alias",
                        left.domain_id, right.domain_id
                    ),
                ));
            }
        }
    }
    Ok(capabilities)
}

/// Read scopes, write scopes and declared targets projected from effect capabilities.
type EffectScopes = (Vec<String>, Vec<String>, Vec<String>);

fn effect_capability_projection(
    capabilities: &[EffectCapabilityMatcher],
) -> Result<EffectScopes, JSpaceError> {
    let mut read_scopes = Vec::new();
    let mut write_scopes = Vec::new();
    let mut declared_targets = Vec::new();
    for capability in capabilities {
        for scope in &capability.read_projection {
            read_scopes.push(rooted_scope(&capability.root, scope)?);
        }
        for scope in &capability.write_projection {
            write_scopes.push(rooted_scope(&capability.root, scope)?);
        }
        for target in &capability.declared_projection {
            declared_targets.push(rooted_scope(&capability.root, target)?);
        }
    }
    Ok((
        canonical_claim_scope_set(read_scopes)?,
        canonical_claim_scope_set(write_scopes)?,
        canonical_claim_scope_set(declared_targets)?,
    ))
}

fn parse_command_templates(
    object: &Map<String, Value>,
    require_domain: bool,
) -> Result<Vec<CommandTemplate>, JSpaceError> {
    let Some(Value::Array(values)) = object.get("command_templates") else {
        return Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            "command_templates",
            "required command template array is missing",
        ));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| {
            let template = value.as_object().ok_or_else(|| {
                JSpaceError::new(
                    "JSPACE_CONTRACT_MALFORMED",
                    "admission",
                    "command_templates",
                    format!("template {index} is not an object"),
                )
            })?;
            let argv = required_string_array(template, "argv")?;
            let effects = required_string_array(template, "effects")?;
            if argv.is_empty() || effects.is_empty() || argv.iter().any(|value| value.is_empty()) {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_TEMPLATE_INVALID",
                    "admission",
                    "command_templates",
                    format!("template {index} requires non-empty argv and effects"),
                ));
            }
            let Some(Value::Array(target_values)) = template.get("targets") else {
                return Err(JSpaceError::new(
                    "JSPACE_CONTRACT_MALFORMED",
                    "admission",
                    "command_templates.targets",
                    "required target array is missing",
                ));
            };
            let targets = target_values
                .iter()
                .map(|target| {
                    let target = target.as_object().ok_or_else(|| {
                        JSpaceError::new(
                            "JSPACE_CONTRACT_MALFORMED",
                            "admission",
                            "command_templates.targets",
                            "target is not an object",
                        )
                    })?;
                    Ok(CommandTarget {
                        domain_id: if require_domain {
                            Some(required_string(target, "domain_id")?)
                        } else {
                            target
                                .get("domain_id")
                                .and_then(Value::as_str)
                                .map(str::to_string)
                        },
                        operation: required_string(target, "operation")?,
                        path: required_string(target, "path")?,
                        argv_index: {
                            if require_domain
                                && target.get("binding").and_then(Value::as_str) != Some("argv")
                            {
                                return Err(JSpaceError::new(
                                    "JSPACE_COMMAND_TARGET_BINDING_INVALID",
                                    "admission",
                                    "command_templates.targets",
                                    "multi-domain targets must bind an exact argv path",
                                ));
                            }
                            required_usize(target, "argv_index")?
                        },
                    })
                })
                .collect::<Result<Vec<_>, JSpaceError>>()?;
            Ok(CommandTemplate {
                argv,
                effects,
                targets,
            })
        })
        .collect()
}

fn validate_command_templates(
    templates: &[CommandTemplate],
    allowed: &HashSet<String>,
    denied: &HashSet<String>,
    declared_targets: &PathTrie,
    effect_capabilities: &[EffectCapabilityMatcher],
    require_domain: bool,
) -> Result<(), JSpaceError> {
    let mut seen = HashSet::new();
    for template in templates {
        if !seen.insert(template.argv.clone()) {
            return Err(JSpaceError::new(
                "JSPACE_COMMAND_TEMPLATE_DUPLICATE",
                "admission",
                &template.argv.join(" "),
                "exact argv template is duplicated",
            ));
        }
        let mut mutation_effects = HashSet::new();
        let mut targeted_mutations = HashSet::new();
        let mut targeted_effects = HashSet::new();
        let mut target_identities = HashSet::new();
        for effect in &template.effects {
            if !KNOWN_OPERATIONS.contains(&effect.as_str()) || effect == "command" {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_EFFECT_INVALID",
                    "admission",
                    effect,
                    "command template contains an unknown effect",
                ));
            }
            if denied.contains(effect)
                || (!require_domain
                    && matches!(effect.as_str(), "read" | "create" | "modify" | "delete")
                    && !allowed.contains(effect))
                || (require_domain
                    && !matches!(effect.as_str(), "read" | "create" | "modify" | "delete"))
            {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_EFFECT_DENIED",
                    effect,
                    &template.argv.join(" "),
                    "command effect is not admitted",
                ));
            }
            if matches!(effect.as_str(), "create" | "modify" | "delete") {
                mutation_effects.insert(effect.clone());
            }
        }
        for target in &template.targets {
            if !matches!(
                target.operation.as_str(),
                "read" | "create" | "modify" | "delete"
            ) || !template.effects.contains(&target.operation)
            {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_TARGET_EFFECT_MISMATCH",
                    &target.operation,
                    &target.path,
                    "command target operation is absent from effects",
                ));
            }
            if require_domain {
                if !Path::new(&target.path).is_absolute()
                    || Path::new(&target.path)
                        .components()
                        .any(|component| component == Component::ParentDir)
                {
                    return Err(JSpaceError::new(
                        "JSPACE_COMMAND_TARGET_INVALID",
                        &target.operation,
                        &target.path,
                        "multi-domain command targets must be exact absolute paths",
                    ));
                }
            } else {
                scope_components(&target.path)?;
            }
            if template.argv.get(target.argv_index) != Some(&target.path) {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_TARGET_ARGV_MISMATCH",
                    &target.operation,
                    &target.path,
                    format!("target path must equal argv[{}]", target.argv_index),
                ));
            }
            if !target_identities.insert((
                target.domain_id.clone(),
                target.operation.clone(),
                target.path.clone(),
                target.argv_index,
            )) {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_TARGET_DUPLICATE",
                    &target.operation,
                    &target.path,
                    "command target identity is duplicated",
                ));
            }
            targeted_effects.insert(target.operation.clone());
            if target.operation != "read" {
                targeted_mutations.insert(target.operation.clone());
            }
            if require_domain {
                let domain_id = target.domain_id.as_deref().ok_or_else(|| {
                    JSpaceError::new(
                        "JSPACE_COMMAND_TARGET_DOMAIN_MISSING",
                        &target.operation,
                        &target.path,
                        "multi-domain targets require an exact domain identity",
                    )
                })?;
                let capability = effect_capabilities
                    .iter()
                    .find(|capability| capability.domain_id == domain_id)
                    .ok_or_else(|| {
                        JSpaceError::new(
                            "JSPACE_COMMAND_TARGET_DOMAIN_UNKNOWN",
                            &target.operation,
                            &target.path,
                            format!("unknown effect domain {domain_id}"),
                        )
                    })?;
                let relative =
                    capability.resolve_target(Path::new(&target.path), &target.operation)?;
                if !capability.allowed_operations.contains(&target.operation) {
                    return Err(JSpaceError::new(
                        "JSPACE_COMMAND_EFFECT_DENIED",
                        &target.operation,
                        &target.path,
                        format!("effect is not admitted in domain {domain_id}"),
                    ));
                }
                let scope_allowed = if target.operation == "read" {
                    capability.read_scopes.matches(&relative)
                } else {
                    capability.write_scopes.matches(&relative)
                };
                if !scope_allowed
                    || (target.operation != "read"
                        && !capability.declared_targets.matches(&relative))
                {
                    return Err(JSpaceError::new(
                        JSPACE_EXPANSION_REQUIRED,
                        &target.operation,
                        &target.path,
                        format!("target is outside exact domain capability {domain_id}"),
                    ));
                }
            } else if target.operation != "read"
                && !declared_targets.matches(&target.path) {
                    return Err(JSpaceError::new(
                        "JSPACE_COMMAND_TARGET_UNDECLARED",
                        &target.operation,
                        &target.path,
                        "command mutation target is not declared",
                    ));
                }
        }
        if mutation_effects != targeted_mutations {
            return Err(JSpaceError::new(
                "JSPACE_COMMAND_MUTATION_TARGET_MISSING",
                "command",
                &template.argv.join(" "),
                "every mutation effect requires an exact target",
            ));
        }
        if require_domain {
            let effects = template.effects.iter().cloned().collect::<HashSet<_>>();
            if effects != targeted_effects {
                return Err(JSpaceError::new(
                    "JSPACE_COMMAND_EFFECT_TARGET_MISMATCH",
                    "command",
                    &template.argv.join(" "),
                    "every effect requires at least one exact domain target",
                ));
            }
            for (argv_index, argument) in template.argv.iter().enumerate().skip(1) {
                let path = Path::new(argument);
                if !path.is_absolute() {
                    continue;
                }
                let is_declared = template
                    .targets
                    .iter()
                    .any(|target| target.argv_index == argv_index && target.path == *argument);
                if !is_declared {
                    return Err(JSpaceError::new(
                        "JSPACE_COMMAND_MUTATION_TARGET_MISSING",
                        "command",
                        argument,
                        format!("absolute argv[{argv_index}] has no exact target identity"),
                    ));
                }
            }
            validate_multi_domain_command_semantics(template)?;
        }
    }
    Ok(())
}

fn validate_multi_domain_command_semantics(template: &CommandTemplate) -> Result<(), JSpaceError> {
    let argv = &template.argv;
    if argv.len() != 6
        || argv[0] != "git"
        || argv[1] != "--git-dir"
        || !Path::new(&argv[2]).is_absolute()
        || argv[3] != "worktree"
        || argv[4] != "remove"
        || !Path::new(&argv[5]).is_absolute()
    {
        return Err(JSpaceError::new(
            "JSPACE_MULTI_DOMAIN_COMMAND_UNSUPPORTED",
            "command",
            &argv.join(" "),
            "multi-domain commands require an exact closed semantic parser",
        ));
    }

    let effects = template
        .effects
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let required_effects = HashSet::from(["read", "delete"]);
    let target_semantics = template
        .targets
        .iter()
        .map(|target| (target.argv_index, target.operation.as_str()))
        .collect::<HashSet<_>>();
    let required_targets = HashSet::from([(2, "read"), (2, "delete"), (5, "delete")]);
    if effects != required_effects
        || target_semantics != required_targets
        || template.targets.len() != required_targets.len()
    {
        return Err(JSpaceError::new(
            "JSPACE_COMMAND_SEMANTICS_MISMATCH",
            "command",
            &argv.join(" "),
            "git worktree remove requires exact read/delete effects on argv[2] and delete on argv[5]",
        ));
    }
    Ok(())
}

fn required_string(object: &Map<String, Value>, key: &str) -> Result<String, JSpaceError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                key,
                "required non-empty string is missing",
            )
        })
}

fn required_usize(object: &Map<String, Value>, key: &str) -> Result<usize, JSpaceError> {
    object
        .get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            JSpaceError::new(
                "JSPACE_CONTRACT_MALFORMED",
                "admission",
                key,
                "required non-negative integer is missing",
            )
        })
}

fn required_string_array(
    object: &Map<String, Value>,
    key: &str,
) -> Result<Vec<String>, JSpaceError> {
    let Some(Value::Array(values)) = object.get(key) else {
        return Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            key,
            "required array of strings is missing",
        ));
    };
    values
        .iter()
        .map(|value| {
            value.as_str().map(str::to_string).ok_or_else(|| {
                JSpaceError::new(
                    "JSPACE_CONTRACT_MALFORMED",
                    "admission",
                    key,
                    "array member is not a string",
                )
            })
        })
        .collect()
}

fn operation_set(values: Vec<String>, field: &str) -> Result<HashSet<String>, JSpaceError> {
    let mut result = HashSet::new();
    for value in values {
        if !KNOWN_OPERATIONS.contains(&value.as_str())
            && !matches!(value.as_str(), "network" | "install" | "system_mutation")
        {
            return Err(JSpaceError::new(
                "JSPACE_UNKNOWN_OPERATION",
                "admission",
                field,
                format!("unknown operation {value}"),
            ));
        }
        result.insert(value);
    }
    Ok(result)
}

fn validate_object_field(object: &Map<String, Value>, key: &str) -> Result<(), JSpaceError> {
    if !object.get(key).is_some_and(Value::is_object) {
        return Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            key,
            "required object is missing",
        ));
    }
    Ok(())
}

fn validate_string_array_field(object: &Map<String, Value>, key: &str) -> Result<(), JSpaceError> {
    required_string_array(object, key).map(|_| ())
}

fn validate_object_array_field(object: &Map<String, Value>, key: &str) -> Result<(), JSpaceError> {
    let Some(Value::Array(values)) = object.get(key) else {
        return Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            key,
            "required object array is missing",
        ));
    };
    if values.iter().all(Value::is_object) {
        Ok(())
    } else {
        Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            key,
            "array member is not an object",
        ))
    }
}

fn validate_expansion_rule(object: &Map<String, Value>) -> Result<(), JSpaceError> {
    let Some(expansion) = object.get("expansion").and_then(Value::as_object) else {
        return Err(JSpaceError::new(
            "JSPACE_CONTRACT_MALFORMED",
            "admission",
            "expansion",
            "expansion rule is missing",
        ));
    };
    if expansion.get("mode").and_then(Value::as_str) != Some("exact_target_only")
        || expansion.get("error_code").and_then(Value::as_str) != Some(JSPACE_EXPANSION_REQUIRED)
        || expansion
            .get("mutation_on_expansion")
            .and_then(Value::as_bool)
            != Some(false)
    {
        return Err(JSpaceError::new(
            "JSPACE_EXPANSION_RULE_INVALID",
            "admission",
            "expansion",
            "exact-target no-mutation rule is invalid",
        ));
    }
    Ok(())
}

fn scope_components(raw_scope: &str) -> Result<(Vec<String>, bool), JSpaceError> {
    let mut scope = raw_scope.trim().replace('\\', "/");
    if scope.is_empty() {
        return Err(JSpaceError::new(
            "JSPACE_SCOPE_INVALID",
            "admission",
            raw_scope,
            "scope must be non-empty",
        ));
    }
    if scope.starts_with('/') || (scope.len() > 1 && scope.as_bytes()[1] == b':') {
        return Err(JSpaceError::new(
            "JSPACE_SCOPE_INVALID",
            "admission",
            raw_scope,
            "scope must be relative to repo_root",
        ));
    }
    let recursive = scope == "**" || scope.ends_with("/**");
    if recursive {
        scope = scope.strip_suffix("/**").unwrap_or_default().to_string();
    }
    if scope.starts_with("./") {
        scope = scope[2..].to_string();
    }
    if scope.contains('*') {
        return Err(JSpaceError::new(
            "JSPACE_SCOPE_INVALID",
            "admission",
            raw_scope,
            "only a terminal /** wildcard is supported",
        ));
    }
    let mut components = Vec::new();
    for component in scope
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
    {
        if component == ".." {
            return Err(JSpaceError::new(
                "JSPACE_PATH_TRAVERSAL",
                "admission",
                raw_scope,
                "scope contains parent traversal",
            ));
        }
        components.push(component.to_string());
    }
    Ok((components, recursive))
}

fn canonical_scope_set(values: Vec<String>) -> Result<Vec<String>, JSpaceError> {
    let scopes = values
        .into_iter()
        .map(|scope| canonical_scope(&scope))
        .collect::<Result<BTreeSet<_>, _>>()?;
    Ok(scopes.into_iter().collect())
}

fn canonical_claim_scope_set(values: Vec<String>) -> Result<Vec<String>, JSpaceError> {
    let scopes = values
        .into_iter()
        .map(|scope| canonical_claim_scope(&scope))
        .collect::<Result<BTreeSet<_>, _>>()?;
    Ok(scopes.into_iter().collect())
}

fn claim_scope_components(raw_scope: &str) -> Result<(bool, Vec<String>, bool), JSpaceError> {
    let scope = raw_scope.trim().replace('\\', "/");
    let absolute = scope.starts_with('/');
    if absolute {
        let relative = scope.trim_start_matches('/');
        let (components, recursive) = scope_components(relative)?;
        if components.is_empty() {
            return Err(JSpaceError::new(
                "JSPACE_DOMAIN_ROOT_TOO_BROAD",
                "admission",
                raw_scope,
                "filesystem root cannot be used as a scope claim",
            ));
        }
        Ok((true, components, recursive))
    } else {
        let (components, recursive) = scope_components(&scope)?;
        Ok((false, components, recursive))
    }
}

fn canonical_claim_scope(raw_scope: &str) -> Result<String, JSpaceError> {
    let (absolute, components, recursive) = claim_scope_components(raw_scope)?;
    let mut exact = components.join("/");
    if absolute {
        exact.insert(0, '/');
    } else if exact.is_empty() {
        exact.push('.');
    }
    if recursive {
        Ok(format!("{exact}/**"))
    } else {
        Ok(exact)
    }
}

fn canonical_scope(raw_scope: &str) -> Result<String, JSpaceError> {
    let (components, recursive) = scope_components(raw_scope)?;
    let exact = if components.is_empty() {
        ".".to_string()
    } else {
        components.join("/")
    };
    if recursive {
        Ok(if components.is_empty() {
            "**".to_string()
        } else {
            format!("{exact}/**")
        })
    } else {
        Ok(exact)
    }
}

fn rooted_scope(root: &Path, relative_scope: &str) -> Result<String, JSpaceError> {
    let relative_scope = canonical_scope(relative_scope)?;
    let rooted = if relative_scope == "." {
        root.display().to_string()
    } else if relative_scope == "**" {
        format!("{}/**", root.display())
    } else if let Some(prefix) = relative_scope.strip_suffix("/**") {
        format!("{}/**", root.join(prefix).display())
    } else {
        root.join(relative_scope).display().to_string()
    };
    canonical_claim_scope(&rooted)
}

fn has_symlink_component(path: &Path) -> Result<bool, JSpaceError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => return Ok(true),
            Ok(_) => {}
            Err(error) => {
                return Err(JSpaceError::new(
                    "JSPACE_DOMAIN_ROOT_INVALID",
                    "admission",
                    &path.display().to_string(),
                    format!("failed to inspect effect domain component: {error}"),
                ));
            }
        }
    }
    Ok(false)
}

fn normalized_root(path: &Path) -> Result<PathBuf, JSpaceError> {
    if !path.exists() || !path.is_dir() {
        return Err(JSpaceError::new(
            "JSPACE_ROOT_INVALID",
            "admission",
            &path.display().to_string(),
            "repo root must be an existing directory",
        ));
    }
    Ok(crate::normalize_path(path))
}

fn resolve_existing_boundary(path: &Path) -> Result<PathBuf, String> {
    let mut missing = Vec::new();
    let mut existing = path.to_path_buf();
    while !existing.exists() {
        let Some(name) = existing.file_name() else {
            return Err(format!("no existing ancestor for {}", path.display()));
        };
        missing.push(name.to_os_string());
        existing.pop();
    }
    let mut resolved = existing
        .canonicalize()
        .map_err(|error| format!("failed to canonicalize {}: {error}", existing.display()))?;
    for component in missing.iter().rev() {
        resolved.push(component);
    }
    Ok(crate::normalize_path(&resolved))
}

fn normalize_command_type(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().replace('-', "_").as_str() {
        "bash" | "zsh" | "shell" | "shells" | "shell_command" | "shll" | "shall" => {
            "shell_command".to_string()
        }
        "apply_patch" => "apply_patch".to_string(),
        "planning" => "planning".to_string(),
        "task_status" => "task_status".to_string(),
        "web_discover" | "web_search" | "web_fetch" => "web_discover".to_string(),
        "generate_media" | "image_gen" | "generate_image" => "generate_media".to_string(),
        "read_media" | "view_media" | "inspect_media" => "read_media".to_string(),
        other => other.to_string(),
    }
}

fn is_known_command_type(command_type: &str) -> bool {
    matches!(
        command_type,
        "shell_command"
            | "bash"
            | "zsh"
            | "apply_patch"
            | "planning"
            | "task_status"
            | "web_discover"
            | "generate_media"
            | "read_media"
            | "source_read"
    )
}

fn shell_command_parts(raw: &str) -> (String, Option<String>) {
    let parsed = serde_json::from_str::<Value>(raw).ok();
    let command = parsed
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|object| object.get("command").or_else(|| object.get("cmd")))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| raw.trim())
        .to_string();
    let workdir = parsed
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|object| object.get("workdir").or_else(|| object.get("cwd")))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    (command, workdir)
}

/// The shell line and optional workdir of a command_run shell entry, accepting the JSON wrapper
/// form `{"command": ..., "workdir": ...}` exactly as admission reads it.
pub fn shell_line_parts(raw: &str) -> (String, Option<String>) {
    shell_command_parts(raw)
}

fn parse_shell_argv(raw: &str) -> Result<Vec<String>, JSpaceError> {
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Quote {
        None,
        Single,
        Double,
    }

    let unsafe_error =
        |detail: &str| JSpaceError::new("JSPACE_COMMAND_SYNTAX_UNSAFE", "command", raw, detail);
    if raw.trim().is_empty() {
        return Err(unsafe_error("command must be non-empty"));
    }
    let mut argv = Vec::new();
    let mut token = String::new();
    let mut token_started = false;
    let mut quote = Quote::None;
    let mut characters = raw.chars().peekable();
    while let Some(character) = characters.next() {
        if matches!(character, '\0' | '\r' | '\n') {
            return Err(unsafe_error(
                "control bytes and multiline shell commands are denied",
            ));
        }
        match quote {
            Quote::Single => {
                if character == '\'' {
                    quote = Quote::None;
                } else {
                    token.push(character);
                }
            }
            Quote::Double => match character {
                '"' => quote = Quote::None,
                '\\' => {
                    let Some(escaped) = characters.next() else {
                        return Err(unsafe_error("trailing escape is invalid"));
                    };
                    token.push(escaped);
                }
                '$' | '`' => {
                    return Err(unsafe_error(
                        "shell expansion and command substitution are denied",
                    ));
                }
                _ => token.push(character),
            },
            Quote::None => match character {
                character if character.is_whitespace() => {
                    if token_started {
                        argv.push(std::mem::take(&mut token));
                        token_started = false;
                    }
                }
                '\'' => {
                    quote = Quote::Single;
                    token_started = true;
                }
                '"' => {
                    quote = Quote::Double;
                    token_started = true;
                }
                '\\' => {
                    let Some(escaped) = characters.next() else {
                        return Err(unsafe_error("trailing escape is invalid"));
                    };
                    if matches!(escaped, '\r' | '\n' | '\0') {
                        return Err(unsafe_error("escaped control bytes are denied"));
                    }
                    token.push(escaped);
                    token_started = true;
                }
                ';' | '&' | '|' | '<' | '>' | '(' | ')' | '$' | '`' | '#' | '*' | '?' | '['
                | ']' | '{' | '}' | '~' => {
                    return Err(unsafe_error(
                        "shell control, expansion, redirection, comments, and globbing are denied",
                    ));
                }
                _ => {
                    token.push(character);
                    token_started = true;
                }
            },
        }
    }
    if quote != Quote::None {
        return Err(unsafe_error("unterminated shell quote is invalid"));
    }
    if token_started {
        argv.push(token);
    }
    if argv.is_empty() {
        return Err(unsafe_error("command must contain an executable"));
    }
    Ok(argv)
}

pub fn semantic_sha256(payload: &Value) -> String {
    let canonical = canonical_json(payload);
    let digest = Sha256::digest(canonical.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => canonical_json_string(value),
        Value::Array(values) => {
            let members = values
                .iter()
                .map(canonical_json)
                .collect::<Vec<_>>()
                .join(",");
            format!("[{members}]")
        }
        Value::Object(object) => {
            let mut keys = object.keys().collect::<Vec<_>>();
            keys.sort();
            let members = keys
                .into_iter()
                .filter_map(|key| {
                    object.get(key).map(|value| {
                        format!("{}:{}", canonical_json_string(key), canonical_json(value))
                    })
                })
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{members}}}")
        }
    }
}

fn canonical_json_string(value: &str) -> String {
    let mut output = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if character.is_control() => {
                output.push_str(&format!("\\u{:04x}", character as u32))
            }
            character if character.is_ascii() => output.push(character),
            character => {
                let mut units = [0u16; 2];
                for unit in character.encode_utf16(&mut units).iter() {
                    output.push_str(&format!("\\u{:04x}", unit));
                }
            }
        }
    }
    output.push('"');
    output
}

#[cfg(test)]
mod tests {
    use super::{
        JSPACE_EXPANSION_REQUIRED, JSpaceAdmissionCache, JSpaceMatcher, JSpaceScopeProjection,
        authorization_semantic_sha256, scope_claims_overlap, scope_projections_conflict,
        semantic_sha256,
    };
    use serde_json::{Value, json};
    use std::fs;

    const AUTHORIZATION_DIGEST_CROSS_LANGUAGE_VECTOR: &str =
        "bd2a325bfeb427c3d3f677d8d831af0f79708c05bf87e9409c1625ba2e16c11f";

    fn contract(root: &std::path::Path) -> Value {
        let mut value = json!({
            "schema_version": "jspace_contract_v2",
            "repo_root": root,
            "dcf_generation": {
                "repo_root": root,
                "generation_id": "g",
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-a"}
                    }
                }
            },
            "provenance": {"matched_surface_ids": ["surface"]},
            "matched_surface_ids": ["surface"],
            "read_scopes": ["src/**"],
            "write_scopes": ["src/**"],
            "allowed_operations": ["read", "create", "modify", "command"],
            "denied_operations": ["network", "install", "system_mutation"],
            "command_templates": [{
                "argv": ["git", "status", "--short"],
                "effects": ["read"],
                "targets": []
            }],
            "focused_verifiers": [],
            "declared_targets": ["src/main.rs"],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            }
        });
        let authorization = authorization_semantic_sha256(&value).expect("authorization digest");
        value["authorization_semantic_sha256"] = Value::String(authorization);
        let content = semantic_sha256(&value);
        value["content_sha256"] = Value::String(content);
        value
    }

    fn reseal(mut value: Value) -> Value {
        value
            .as_object_mut()
            .expect("object")
            .remove("content_sha256");
        let authorization = authorization_semantic_sha256(&value).expect("authorization digest");
        value["authorization_semantic_sha256"] = Value::String(authorization);
        let content = semantic_sha256(&value);
        value["content_sha256"] = Value::String(content);
        value
    }

    fn inspect_contract(root: &std::path::Path) -> Value {
        let mut value = contract(root);
        value["dcf_generation"] = json!({"repo_root": root, "context_mode": "inspect_workspace_jspace",
            "dcf_available": false, "required_domain_bindings": {}});
        value["matched_surface_ids"] = json!([]);
        value["provenance"] = json!({"context_mode": "inspect_workspace_jspace", "authority": "parent_task"});
        value["read_scopes"] = json!([]);
        value["write_scopes"] = json!([]);
        value["declared_targets"] = json!([]);
        value["command_templates"] = json!([]);
        value["allowed_operations"] = json!(["command", "read"]);
        value["denied_operations"] = json!(["create", "delete", "modify", "network"]);
        value["inspect_shell"] = json!({"schema_version": "nokiy_inspect_shell_v1",
            "profile_sha256": "a".repeat(64), "evidence_root": root.join("evidence"), "network": "loopback"});
        reseal(value)
    }

    #[test]
    fn inspect_shell_admits_any_shell_line_only_with_a_bound_profile() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let matcher = JSpaceMatcher::from_value(&root, &inspect_contract(&root)).unwrap();
        // No profile is bound to the test process, so admission fails closed.
        for line in ["rg -n needle src | head -3", "python3 -c 'print(1)'", "cat <<'EOF'\nx\nEOF"] {
            let error = matcher.check_command("bash", line).unwrap_err();
            assert_eq!(error.code(), "JSPACE_INSPECT_DENIED", "{line}");
        }
        assert!(matcher.check_path("modify", &root.join("src/main.rs")).is_err());
        assert!(matcher.check_path("create", &root.join("new.txt")).is_err());
    }

    #[test]
    fn inspect_mutation_refusal_names_the_admitted_shell_route() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let matcher = JSpaceMatcher::from_value(&root, &inspect_contract(&root)).unwrap();
        let route = format!("shell lines under {}", root.join("evidence").display());
        for operation in ["create", "modify", "delete"] {
            let error = matcher
                .check_path(operation, &root.join("evidence/prepare.py"))
                .unwrap_err();
            assert_eq!(error.code(), "JSPACE_OPERATION_DENIED");
            assert!(error.to_string().contains(&route), "{error}");
        }
        // Reads and ordinary contracts keep the plain reason.
        assert_eq!(matcher.denied_detail("read", "plain"), "plain");
        let ordinary = JSpaceMatcher::from_value(&root, &contract(&root)).unwrap();
        assert_eq!(ordinary.denied_detail("create", "plain"), "plain");
    }

    #[test]
    fn inspect_shell_requires_a_read_command_only_contract_and_is_digest_bound() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let base = inspect_contract(&root);
        for (field, value) in [
            ("write_scopes", json!(["src/**"])),
            ("allowed_operations", json!(["command", "modify", "read"])),
            ("allowed_operations", json!(["read"])),
        ] {
            let mut changed = base.clone();
            changed[field] = value;
            if field == "allowed_operations" {
                changed["denied_operations"] = json!(["delete", "network"]);
            }
            assert!(JSpaceMatcher::from_value(&root, &reseal(changed)).is_err(), "{field}");
        }
        let mut multi_domain = base.clone();
        multi_domain["schema_version"] = json!("jspace_contract_v3");
        assert!(JSpaceMatcher::from_value(&root, &multi_domain).is_err(), "v3 contracts carry no inspect shell");
        let mut tampered = base.clone();
        tampered["inspect_shell"]["network"] = json!("none");
        tampered["content_sha256"] = json!(semantic_sha256(&{
            let mut unsealed = tampered.clone();
            unsealed.as_object_mut().unwrap().remove("content_sha256");
            unsealed
        }));
        assert!(JSpaceMatcher::from_value(&root, &tampered).is_err(), "authorization digest binds the policy");
        let ordinary = JSpaceMatcher::from_value(&root, &contract(&root)).unwrap();
        assert!(ordinary.check_command("bash", "rg -n needle src | head -3").is_err());
    }

    fn parent_shell_contract(root: &std::path::Path) -> Value {
        let mut value = contract(root);
        value["parent_shell"] = json!({"schema_version": "nokiy_parent_shell_v1",
            "profile_sha256": "b".repeat(64), "evidence_root": root.join("evidence"), "network": "inherit"});
        reseal(value)
    }

    #[test]
    fn parent_shell_routes_every_line_but_an_exact_template_to_the_parent() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let matcher = JSpaceMatcher::from_value(&root, &parent_shell_contract(&root)).unwrap();
        assert!(matcher.parent_shell_admitted());
        for line in ["rg -n needle src | head -3", "python3 -c 'print(1)'", "git diff --check",
            "cat <<'EOF'\nx\nEOF", r#"{"command":"ls -la","workdir":"src"}"#] {
            matcher.check_command("bash", line).unwrap_or_else(|error| panic!("{line}: {error}"));
            assert!(matcher.parent_shell_routes("zsh", line), "{line}");
        }
        // The exact template keeps its in-engine grant and effects.
        matcher.check_command("bash", "git status --short").unwrap();
        assert!(!matcher.parent_shell_routes("bash", "git status --short"));
        // Other tools never route to the parent; edits keep their exact grants.
        assert!(!matcher.parent_shell_routes("apply_patch", "*** Begin Patch"));
        assert!(!matcher.parent_shell_routes("source_read", "src/main.rs"));
        assert!(matcher.check_path("modify", &root.join("src/main.rs")).is_ok());
        assert!(matcher.check_path("modify", &root.join("README.md")).is_err());
        // Without the policy the same lines stay refused.
        let ordinary = JSpaceMatcher::from_value(&root, &contract(&root)).unwrap();
        assert!(!ordinary.parent_shell_admitted());
        assert!(!ordinary.parent_shell_routes("bash", "git diff --check"));
        assert!(ordinary.check_command("bash", "git diff --check").is_err());
    }

    #[test]
    fn parent_shell_admission_needs_read_and_command_and_is_digest_bound() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let base = parent_shell_contract(&root);
        let mut no_command = base.clone();
        no_command["allowed_operations"] = json!(["read", "create", "modify"]);
        no_command["command_templates"] = json!([]);
        assert_eq!(
            JSpaceMatcher::from_value(&root, &reseal(no_command)).unwrap_err().code(),
            "JSPACE_PARENT_SHELL_DENIED"
        );
        let mut both = base.clone();
        both["inspect_shell"] = json!({"schema_version": "nokiy_inspect_shell_v1",
            "profile_sha256": "a".repeat(64), "evidence_root": root.join("evidence"), "network": "inherit"});
        assert!(JSpaceMatcher::from_value(&root, &reseal(both)).is_err());
        let mut tampered = base.clone();
        tampered["parent_shell"]["network"] = json!("none");
        tampered["content_sha256"] = json!(semantic_sha256(&{
            let mut unsealed = tampered.clone();
            unsealed.as_object_mut().unwrap().remove("content_sha256");
            unsealed
        }));
        assert!(JSpaceMatcher::from_value(&root, &tampered).is_err(), "authorization digest binds the policy");
        // A parent shell alone satisfies the command operation without templates.
        let mut bare = base.clone();
        bare["command_templates"] = json!([]);
        JSpaceMatcher::from_value(&root, &reseal(bare)).expect("parent shell admits command without templates");
    }

    fn effect_contract(
        session_root: &std::path::Path,
        worktree_root: &std::path::Path,
        metadata_root: &std::path::Path,
    ) -> Value {
        let session_root = fs::canonicalize(session_root).expect("canonical session root");
        let worktree_root = fs::canonicalize(worktree_root).expect("canonical worktree root");
        let metadata_root = fs::canonicalize(metadata_root).expect("canonical metadata root");
        let metadata_target = metadata_root.join("worktrees/task");
        let mut value = json!({
            "schema_version": "jspace_contract_v3",
            "repo_root": session_root,
            "dcf_generation": {
                "repo_root": session_root,
                "generation_id": "g-effect",
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-effect"}
                    }
                }
            },
            "provenance": {"matched_surface_ids": ["surface"]},
            "matched_surface_ids": ["surface"],
            "allowed_operations": ["command"],
            "denied_operations": ["network", "install", "system_mutation"],
            "effect_capabilities": [{
                "domain_id": "git_metadata",
                "root": metadata_target,
                "read_scopes": ["."],
                "write_scopes": ["."],
                "allowed_operations": ["read", "delete"],
                "declared_targets": ["."]
            }, {
                "domain_id": "worktree",
                "root": worktree_root,
                "read_scopes": [],
                "write_scopes": ["."],
                "allowed_operations": ["delete"],
                "declared_targets": ["."]
            }],
            "command_templates": [{
                "argv": [
                    "git", "--git-dir", metadata_target, "worktree", "remove", worktree_root
                ],
                "effects": ["read", "delete"],
                "targets": [{
                    "domain_id": "git_metadata",
                    "operation": "read",
                    "path": metadata_target,
                    "binding": "argv",
                    "argv_index": 2
                }, {
                    "domain_id": "git_metadata",
                    "operation": "delete",
                    "path": metadata_target,
                    "binding": "argv",
                    "argv_index": 2
                }, {
                    "domain_id": "worktree",
                    "operation": "delete",
                    "path": worktree_root,
                    "binding": "argv",
                    "argv_index": 5
                }]
            }],
            "focused_verifiers": [],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            }
        });
        let authorization = authorization_semantic_sha256(&value).expect("effect authorization");
        value["authorization_semantic_sha256"] = Value::String(authorization);
        let content = semantic_sha256(&value);
        value["content_sha256"] = Value::String(content);
        value
    }

    fn effect_topology() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
        let root = tempfile::tempdir().expect("effect topology");
        let worktree = root.path().join("worktree-domain");
        let metadata = root.path().join("metadata-domain");
        fs::create_dir(&worktree).expect("worktree domain");
        fs::create_dir(&metadata).expect("metadata domain");
        fs::create_dir_all(metadata.join("worktrees/task")).expect("common metadata target");
        (root, worktree, metadata)
    }

    fn verifier_fixture() -> (tempfile::TempDir, tempfile::TempDir, Value) {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::PermissionsExt;
        let workspace = tempfile::tempdir().expect("workspace");
        let artifacts = tempfile::tempdir().expect("artifacts");
        let workspace_path = fs::canonicalize(workspace.path()).expect("canonical workspace");
        let artifact_path = fs::canonicalize(artifacts.path()).expect("canonical artifact root");
        fs::create_dir(workspace_path.join("src")).expect("src");
        let pinned = workspace_path.join("src/test.rs");
        fs::write(&pinned, b"test entry").expect("pinned entry");
        let binary = workspace_path.join("runner");
        fs::write(&binary, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).expect("binary");
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).expect("executable");
        let mut value = contract(&workspace_path);
        value["read_scopes"] = json!(["src/test.rs"]);
        value["write_scopes"] = json!([]);
        value["declared_targets"] = json!([]);
        value["command_templates"] = json!([]);
        value["source_read"] = json!(true);
        value["verifier_artifact_root"] = json!(artifact_path);
        value["verifier_commands"] = json!([{
            "argv": [binary, pinned],
            "executable_sha256": format!("{:x}", Sha256::digest(fs::read(&binary).unwrap())),
            "pinned_files": [{"path": pinned, "sha256": format!("{:x}", Sha256::digest(fs::read(&pinned).unwrap()))}],
            "timeout_seconds": 30, "scratch_root": artifact_path.join("scratch"), "network": false
        }]);
        (workspace, artifacts, value)
    }

    #[test]
    fn work_product_verifiers_need_no_pin_but_unwritten_programs_do() {
        let (workspace, _artifacts, value) = verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let binary = value["verifier_commands"][0]["argv"][0].clone();
        let (written, entry) = (root.join("src/new.rs"), root.join("src/test.rs"));
        let with = |argv: Value| {
            let mut changed = value.clone();
            changed["write_scopes"] = json!(["src/new.rs"]);
            changed["declared_targets"] = json!(["src/new.rs"]);
            changed["verifier_commands"][0]["argv"] = argv;
            changed["verifier_commands"][0]["pinned_files"] = json!([]);
            JSpaceMatcher::from_value(workspace.path(), &reseal(changed))
        };
        // The worker writes new.rs (not yet created): checking it needs no pin.
        with(json!([&binary, "--check", &written])).expect("work-product verifier admitted unpinned");
        fs::write(&written, b"draft").unwrap();
        with(json!([&binary, &written])).expect("existing work product still needs no pin");
        // An unwritten existing operand is a program or input the coordinator can pin.
        for argv in [json!([&binary, &entry]), json!([&binary, &written, &entry]), json!([&binary, "test"])] {
            assert_eq!(with(argv.clone()).unwrap_err().code(), "JSPACE_VERIFIER_GRANT_MALFORMED", "{argv}");
        }
    }

    #[test]
    fn admitted_source_digest_requires_exact_readonly_unique_pin() {
        use std::path::Path;
        let (workspace, _artifacts, value) = verifier_fixture();
        let admitted = JSpaceMatcher::from_value(workspace.path(), &reseal(value)).unwrap();
        let expected = admitted.verifier_commands[0].pinned_files[0].sha256.clone();
        assert_eq!(admitted.admitted_source_read_digest(Path::new("src/test.rs")).unwrap(), expected);
        for name in ["src/other.rs", "../src/test.rs", "/src/test.rs"] {
            assert!(admitted.admitted_source_read_digest(Path::new(name)).is_err());
        }
        // Exercise cached admission independently of contract decoding.
        for mode in ["missing", "conflict", "invalid", "writable", "wildcard", "disabled", "verifier_only"] {
            let mut matcher = admitted.clone();
            match mode {
                "missing" => matcher.verifier_commands.clear(),
                "conflict" => {
                    let mut duplicate = matcher.verifier_commands[0].clone();
                    duplicate.pinned_files[0].sha256 = "0".repeat(64);
                    matcher.verifier_commands.push(duplicate);
                },
                "invalid" => matcher.verifier_commands[0].pinned_files[0].sha256 = "short".into(),
                "writable" => matcher.scope_projection.write_scopes = vec!["src/**".into()],
                "wildcard" => matcher.scope_projection.read_scopes = vec!["src/**".into()],
                "disabled" => matcher.source_read = false,
                "verifier_only" => matcher.verifier_commands[0].pinned_files[0].scope =
                    Some(super::VerifierPinScope::VerifierOnly),
                _ => unreachable!(),
            }
            assert!(matcher.admitted_source_read_digest(Path::new("src/test.rs")).is_err(), "{mode}");
        }
    }

    fn verifier_only_fixture() -> (tempfile::TempDir, tempfile::TempDir, Value) {
        use sha2::{Digest, Sha256};
        let (workspace, artifacts, mut value) = verifier_fixture();
        let entry = fs::canonicalize(artifacts.path()).unwrap().join("acceptance.py");
        fs::write(&entry, b"print('acceptance')\n").unwrap();
        value["verifier_commands"][0]["argv"][1] = json!(entry);
        value["verifier_commands"][0]["pinned_files"][0] = json!({
            "path": entry,
            "sha256": format!("{:x}", Sha256::digest(fs::read(&entry).unwrap())),
            "scope": "verifier_only",
        });
        (workspace, artifacts, value)
    }

    #[test]
    fn verifier_only_pin_preserves_legacy_serialization_and_authorization_binding() {
        let (workspace, _artifacts, value) = verifier_fixture();
        let legacy = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        assert!(legacy.verifier_commands()[0].pinned_files[0].scope.is_none());
        assert_eq!(serde_json::to_value(legacy.verifier_commands()).unwrap(), value["verifier_commands"]);

        let (external_workspace, _external_artifacts, external) = verifier_only_fixture();
        let sealed = reseal(external.clone());
        let matcher = JSpaceMatcher::from_value(external_workspace.path(), &sealed).unwrap();
        assert_eq!(serde_json::to_value(matcher.verifier_commands()).unwrap(), external["verifier_commands"]);
        let mut changed = sealed;
        changed["verifier_commands"][0]["pinned_files"][0].as_object_mut().unwrap().remove("scope");
        assert_ne!(authorization_semantic_sha256(&changed).unwrap(), matcher.authorization_semantic_sha256());
        assert_eq!(JSpaceMatcher::from_value(external_workspace.path(), &changed).unwrap_err().code(),
            "JSPACE_AUTHORIZATION_DIGEST_MISMATCH");
        assert_eq!(JSpaceMatcher::from_value(external_workspace.path(), &reseal(changed)).unwrap_err().code(),
            "JSPACE_PATH_OUTSIDE_ROOT");
    }

    #[test]
    fn verifier_only_pin_grants_no_source_read_and_revalidates_sha() {
        use std::path::Path;
        let (workspace, _artifacts, value) = verifier_only_fixture();
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        let command = matcher.check_verifier_command(0).unwrap();
        let entry = &command.pinned_files[0].path;
        assert_eq!(matcher.check_path("read", entry).unwrap_err().code(), "JSPACE_PATH_OUTSIDE_ROOT");
        assert_eq!(matcher.check_source_read(entry).unwrap_err().code(), "JSPACE_PATH_OUTSIDE_ROOT");
        assert!(matcher.admitted_source_read_digest(Path::new("src/test.rs")).is_err());
        assert!(matcher.admitted_source_read_digest(entry).is_err());
        fs::create_dir(&command.scratch_root).unwrap();
        matcher.revalidate_verifier_paths().unwrap();
        fs::write(entry, b"changed acceptance\n").unwrap();
        assert_eq!(matcher.revalidate_verifier_paths().unwrap_err().code(), "NOKIY_LOCAL_CONTEXT_STALE");
        assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(value)).unwrap_err().code(),
            "JSPACE_VERIFIER_FILE_MISMATCH");
    }

    #[test]
    fn verifier_only_pin_rejects_invalid_scope_argv_digest_and_workspace_paths() {
        let (workspace, _artifacts, value) = verifier_only_fixture();
        for scope in [Value::Null, json!(""), json!("source_read"), json!("workspace"), json!(true), json!({}), json!({"verifier_only": null})] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["pinned_files"][0]["scope"] = scope;
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED");
        }
        for mode in ["duplicate_argv", "missing_argv", "duplicate_pin", "short_digest", "uppercase_digest"] {
            let mut invalid = value.clone();
            match mode {
                "duplicate_argv" => {
                    let path = invalid["verifier_commands"][0]["argv"][1].clone();
                    invalid["verifier_commands"][0]["argv"].as_array_mut().unwrap().push(path);
                }
                "missing_argv" => invalid["verifier_commands"][0]["argv"][1] = json!("other.py"),
                "duplicate_pin" => {
                    let pin = invalid["verifier_commands"][0]["pinned_files"][0].clone();
                    invalid["verifier_commands"][0]["pinned_files"].as_array_mut().unwrap().push(pin);
                }
                "short_digest" => invalid["verifier_commands"][0]["pinned_files"][0]["sha256"] = json!("a"),
                "uppercase_digest" => invalid["verifier_commands"][0]["pinned_files"][0]["sha256"] = json!("A".repeat(64)),
                _ => unreachable!(),
            }
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED", "{mode}");
        }
        let (legacy_workspace, _legacy_artifacts, mut legacy) = verifier_fixture();
        legacy["verifier_commands"][0]["pinned_files"][0]["scope"] = json!("verifier_only");
        assert_eq!(JSpaceMatcher::from_value(legacy_workspace.path(), &reseal(legacy)).unwrap_err().code(),
            "JSPACE_VERIFIER_PIN_SCOPE_INVALID");
    }

    #[test]
    fn verifier_only_pin_rejects_every_command_scratch_in_either_order() {
        let (workspace, _artifacts, value) = verifier_only_fixture();
        let root = std::path::Path::new(value["verifier_artifact_root"].as_str().unwrap());
        let scratch = root.join("mutable");
        fs::create_dir(&scratch).unwrap();
        let entry = scratch.join("acceptance.py");
        fs::copy(value["verifier_commands"][0]["argv"][1].as_str().unwrap(), &entry).unwrap();
        for mode in ["own", "other_after", "other_before"] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][1] = json!(entry);
            invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(entry);
            if mode == "own" {
                invalid["verifier_commands"][0]["scratch_root"] = json!(scratch);
            } else {
                let mut other = value["verifier_commands"][0].clone();
                other["scratch_root"] = json!(scratch);
                let commands = invalid["verifier_commands"].as_array_mut().unwrap();
                if mode == "other_before" { commands.insert(0, other); } else { commands.push(other); }
            }
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_PIN_SCOPE_INVALID", "{mode}");
        }
    }

    #[test]
    fn verifier_only_pin_requires_canonical_regular_file_without_symlink_ancestry() {
        use std::os::unix::fs::symlink;
        let (workspace, _artifacts, value) = verifier_only_fixture();
        let root = std::path::Path::new(value["verifier_artifact_root"].as_str().unwrap());
        let entry = std::path::Path::new(value["verifier_commands"][0]["argv"][1].as_str().unwrap());
        let link = root.join("entry-link.py");
        symlink(entry, &link).unwrap();
        let parent_link = root.join("parent-link");
        symlink(root, &parent_link).unwrap();
        let git_metadata = root.join(".git");
        fs::create_dir(&git_metadata).unwrap();
        let git_entry = git_metadata.join("acceptance.py");
        fs::copy(entry, &git_entry).unwrap();
        for (path, code) in [
            (link, "JSPACE_VERIFIER_SYMLINK_DENIED"),
            (parent_link.join("acceptance.py"), "JSPACE_VERIFIER_SYMLINK_DENIED"),
            (root.join("./acceptance.py"), "JSPACE_VERIFIER_PATH_INVALID"),
            (root.join("missing.py"), "JSPACE_VERIFIER_PATH_INVALID"),
            (git_entry, "JSPACE_VERIFIER_PIN_SCOPE_INVALID"),
            (root.to_path_buf(), "JSPACE_VERIFIER_GRANT_MALFORMED"),
        ] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][1] = json!(path);
            invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(path);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(), code);
        }
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        fs::create_dir(&matcher.verifier_commands()[0].scratch_root).unwrap();
        fs::remove_file(entry).unwrap();
        symlink(workspace.path().join("src/test.rs"), entry).unwrap();
        assert_eq!(matcher.revalidate_verifier_paths().unwrap_err().code(), "JSPACE_VERIFIER_SYMLINK_DENIED");
    }

    fn node_verifier_fixture() -> (tempfile::TempDir, tempfile::TempDir, Value) {
        use sha2::{Digest, Sha256};
        let (workspace, artifacts, mut value) = verifier_only_fixture();
        let root = fs::canonicalize(artifacts.path()).unwrap();
        let bin = root.join("bin");
        fs::create_dir(&bin).unwrap();
        let executable = bin.join("node");
        fs::rename(value["verifier_commands"][0]["argv"][0].as_str().unwrap(), &executable).unwrap();
        let entry = root.join("npm-cli.js");
        fs::write(&entry, b"#!/usr/bin/env node\nconsole.log('acceptance');\n").unwrap();
        value["verifier_commands"][0]["argv"] = json!([executable, entry, "test"]);
        value["verifier_commands"][0]["pinned_files"][0]["path"] = json!(entry);
        value["verifier_commands"][0]["pinned_files"][0]["sha256"] =
            json!(format!("{:x}", Sha256::digest(fs::read(&entry).unwrap())));
        (workspace, artifacts, value)
    }

    #[test]
    fn env_node_program_admits_only_explicit_data_pin_and_revalidates_hash() {
        for extension in ["js", "cjs", "mjs"] {
            let (workspace, _artifacts, mut value) = node_verifier_fixture();
            let original = std::path::Path::new(value["verifier_commands"][0]["argv"][1].as_str().unwrap());
            let entry = original.with_extension(extension);
            if entry != original { fs::rename(original, &entry).unwrap(); }
            value["verifier_commands"][0]["argv"][1] = json!(entry);
            value["verifier_commands"][0]["pinned_files"][0]["path"] = json!(entry);
            let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
            assert_eq!(serde_json::to_value(matcher.verifier_commands()).unwrap(), value["verifier_commands"]);
            assert_eq!(matcher.check_source_read(&entry).unwrap_err().code(), "JSPACE_PATH_OUTSIDE_ROOT");
            fs::create_dir(&matcher.verifier_commands()[0].scratch_root).unwrap();
            matcher.revalidate_verifier_paths().unwrap();
            fs::write(&entry, b"#!/usr/bin/env node\nconsole.log('changed');\n").unwrap();
            assert_eq!(matcher.revalidate_verifier_paths().unwrap_err().code(), "NOKIY_LOCAL_CONTEXT_STALE");
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(value)).unwrap_err().code(),
                "JSPACE_VERIFIER_FILE_MISMATCH");
        }
    }

    #[test]
    fn env_node_program_rejects_flags_alternate_entries_and_implicit_scope() {
        let (workspace, artifacts, value) = node_verifier_fixture();
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        fs::create_dir(&matcher.verifier_commands()[0].scratch_root).unwrap();
        for flag in ["--no-warnings", "--require", "-e", "--"] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"].as_array_mut().unwrap().insert(1, json!(flag));
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED", "{flag}");
            let mut cached = matcher.clone();
            cached.verifier_commands[0].argv.insert(1, flag.into());
            assert_eq!(cached.revalidate_verifier_paths().unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED", "{flag}");
        }
        for alternate in ["entry.txt", "entry.py"] {
            let entry = fs::canonicalize(artifacts.path()).unwrap().join(alternate);
            fs::copy(value["verifier_commands"][0]["argv"][1].as_str().unwrap(), &entry).unwrap();
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][1] = json!(entry);
            invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(entry);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED");
        }
        let root = fs::canonicalize(artifacts.path()).unwrap();
        for name in ["nodejs", "env"] {
            let executable = root.join(name);
            fs::copy(value["verifier_commands"][0]["argv"][0].as_str().unwrap(), &executable).unwrap();
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][0] = json!(executable);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED");
        }
        let mut implicit = value.clone();
        implicit["verifier_commands"][0]["pinned_files"][0].as_object_mut().unwrap().remove("scope");
        assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(implicit)).unwrap_err().code(),
            "JSPACE_PATH_OUTSIDE_ROOT");
        let mut duplicate = value;
        let entry = duplicate["verifier_commands"][0]["argv"][1].clone();
        duplicate["verifier_commands"][0]["argv"].as_array_mut().unwrap().push(entry);
        assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(duplicate)).unwrap_err().code(),
            "JSPACE_VERIFIER_GRANT_MALFORMED");
    }

    #[test]
    fn interpreter_entry_headers_are_data_but_exec_operand_headers_are_not() {
        use sha2::{Digest, Sha256};
        let (workspace, _artifacts, mut value) = node_verifier_fixture();
        let entry = std::path::PathBuf::from(value["verifier_commands"][0]["argv"][1].as_str().unwrap());
        let node = std::path::PathBuf::from(value["verifier_commands"][0]["argv"][0].as_str().unwrap());
        let runner = node.with_file_name("runner");
        fs::copy(&node, &runner).unwrap();
        let mut exec_operand = value.clone();
        exec_operand["verifier_commands"][0]["argv"][0] = json!(runner);
        for header in ["#!/usr/bin/env -S node", "#!/usr/bin/env node --no-warnings",
            "#!/usr/bin/env  node", "#! /usr/bin/env node", "#!/usr/bin/env node\r", "#!/bin/sh",
            "#!/usr/bin/env python3"] {
            let bytes = format!("{header}\nconsole.log('acceptance');\n");
            fs::write(&entry, &bytes).unwrap();
            let digest = json!(format!("{:x}", Sha256::digest(bytes.as_bytes())));
            value["verifier_commands"][0]["pinned_files"][0]["sha256"] = digest.clone();
            exec_operand["verifier_commands"][0]["pinned_files"][0]["sha256"] = digest;
            // Node opens the program as source: its #! line is a comment, at admission and at run.
            let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
            let scratch = &matcher.verifier_commands()[0].scratch_root;
            if !scratch.exists() { fs::create_dir(scratch).unwrap(); }
            matcher.revalidate_verifier_paths().unwrap();
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(exec_operand.clone())).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED", "{header:?}");
        }
        fs::write(&entry, b"console.log('no header');\n").unwrap();
        exec_operand["verifier_commands"][0]["pinned_files"][0]["sha256"] =
            json!(format!("{:x}", Sha256::digest(fs::read(&entry).unwrap())));
        JSpaceMatcher::from_value(workspace.path(), &reseal(exec_operand)).unwrap();
        assert!(super::verifier_interpreter_reads_entries(&["/opt/bin/python3.11".into()]));
        assert!(super::verifier_interpreter_reads_entries(&["/opt/bin/python".into()]));
        assert!(!super::verifier_interpreter_reads_entries(&["/opt/bin/python3-config".into()]));
        assert!(!super::verifier_interpreter_reads_entries(&["/opt/bin/nodejs".into()]));
    }

    /// Diagnostic: NOKIY_CONTRACTS=<jspace.json>:... admits prepared contracts as the runtime does.
    #[test]
    #[ignore]
    fn admit_recorded_contracts() {
        for path in std::env::var("NOKIY_CONTRACTS").unwrap().split(':') {
            let value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
            let root = std::path::PathBuf::from(value["repo_root"].as_str().unwrap());
            match JSpaceMatcher::from_value(&root, &value) {
                Ok(matcher) => println!("ADMITTED {path} verifiers={}", matcher.verifier_commands().len()),
                Err(error) => println!("REFUSED {path} {} {error}", error.code()),
            }
        }
    }

    #[test]
    fn env_node_program_keeps_scope_symlink_and_credential_boundaries() {
        let (workspace, artifacts, value) = node_verifier_fixture();
        let root = fs::canonicalize(artifacts.path()).unwrap();
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        let grant = &matcher.verifier_commands()[0];
        fs::create_dir(&grant.scratch_root).unwrap();
        let source = &grant.pinned_files[0].path;
        for (directory, code) in [(root.join(".git"), "JSPACE_VERIFIER_PIN_SCOPE_INVALID"),
            (root.join(".ssh"), "JSPACE_VERIFIER_GRANT_MALFORMED"),
            (grant.scratch_root.clone(), "JSPACE_VERIFIER_PIN_SCOPE_INVALID")] {
            fs::create_dir_all(&directory).unwrap();
            let entry = directory.join("entry.js");
            fs::copy(source, &entry).unwrap();
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][1] = json!(entry);
            invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(entry);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(), code);
            let mut cached = matcher.clone();
            cached.verifier_commands[0].argv[1] = entry.to_str().unwrap().into();
            cached.verifier_commands[0].pinned_files[0].path = entry;
            assert_eq!(cached.revalidate_verifier_paths().unwrap_err().code(), code);
        }
        std::os::unix::fs::symlink(&root, root.join("linked")).unwrap();
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["argv"][1] = json!(root.join("linked/npm-cli.js"));
        invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(root.join("linked/npm-cli.js"));
        assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
            "JSPACE_VERIFIER_SYMLINK_DENIED");
        for directory in [root.join(".ssh"), grant.scratch_root.clone()] {
            let executable = directory.join("node");
            fs::copy(&grant.argv[0], &executable).unwrap();
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"][0] = json!(executable);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED");
        }
    }

    fn python_verifier_fixture() -> (tempfile::TempDir, tempfile::TempDir, Value) {
        let (workspace, artifacts, mut value) = verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let executable = root.join("python3.13");
        fs::rename(value["verifier_commands"][0]["argv"][0].as_str().unwrap(), &executable).unwrap();
        value["verifier_commands"][0]["argv"][0] = json!(executable);
        value["verifier_commands"][0]["python_import_roots"] = json!([root.join("src")]);
        (workspace, artifacts, reseal(value))
    }

    #[test]
    fn python_import_roots_preserve_legacy_shape_and_do_not_grant_reads() {
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let matcher = JSpaceMatcher::from_value(workspace.path(), &value).unwrap();
        let grant = matcher.check_verifier_command(0).unwrap();
        assert_eq!(serde_json::to_value(grant).unwrap(), value["verifier_commands"][0]);
        let root = &grant.python_import_roots.as_ref().unwrap()[0];
        assert_eq!(matcher.check_path("read", root).unwrap_err().code(), JSPACE_EXPANSION_REQUIRED);
        fs::create_dir(&grant.scratch_root).unwrap();
        matcher.revalidate_verifier_paths().unwrap();

        let mut legacy = value.clone();
        legacy["verifier_commands"][0].as_object_mut().unwrap().remove("python_import_roots");
        let legacy = reseal(legacy);
        let old = JSpaceMatcher::from_value(workspace.path(), &legacy).unwrap();
        assert!(old.verifier_commands()[0].python_import_roots.is_none());
        let serialized = serde_json::to_value(old.verifier_commands()).unwrap();
        assert_eq!(serialized, legacy["verifier_commands"]);
        let mut roundtrip = legacy.clone();
        roundtrip["verifier_commands"] = serialized;
        assert_eq!(authorization_semantic_sha256(&roundtrip).unwrap(), old.authorization_semantic_sha256());
        assert_ne!(old.authorization_semantic_sha256(), matcher.authorization_semantic_sha256());

        let mut recursive = value;
        recursive["read_scopes"] = json!(["src/**"]);
        recursive.as_object_mut().unwrap().remove("source_read");
        JSpaceMatcher::from_value(workspace.path(), &reseal(recursive)).unwrap();
    }

    // 2026-10-22 (Nokiy Ultra i8-i10): workers could not run the tests they wrote, and a harness loads
    // test modules by name (unittest) or collects changing files (pytest), so neither pins argv files.
    #[test]
    fn module_test_runners_need_no_pin_and_other_runners_still_do() {
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let python = value["verifier_commands"][0]["argv"][0].clone();
        let pinned = value["verifier_commands"][0]["argv"][1].clone();
        let with = |argv: serde_json::Value, pins: serde_json::Value| {
            let mut changed = value.clone();
            changed["verifier_commands"][0]["argv"] = argv;
            changed["verifier_commands"][0]["pinned_files"] = pins;
            JSpaceMatcher::from_value(workspace.path(), &reseal(changed))
        };
        for argv in [json!([&python, "-B", "-m", "unittest", "-v", "test"]), json!([&python, "-m", "unittest", "test"]),
            json!([&python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "src/test.rs"])] {
            let matcher = with(argv.clone(), json!([])).expect("unpinned module test runner admitted");
            assert!(matcher.check_verifier_command(0).unwrap().pinned_files.is_empty(), "{argv}");
        }
        let runner = value["verifier_commands"][0]["argv"][0].as_str().unwrap().replace("python3.13", "runner");
        // fs::copy keeps the executable mode, so only the interpreter name differs.
        fs::copy(python.as_str().unwrap(), &runner).unwrap();
        for argv in [
            json!([&python, &pinned]),
            json!([&python, "-m", "pytest_extra", "test"]),
            json!([&python, "-m", "nose", "test"]),
            json!([&python, "-I", "-m", "unittest", "test"]),
            json!([&python, "-m", "unittest_extra", "test"]),
            json!([&runner, "-m", "unittest", "test"]),
        ] {
            let error = with(argv.clone(), json!([])).err().expect("unpinned runner must stay refused");
            assert_eq!(error.code(), "JSPACE_VERIFIER_GRANT_MALFORMED", "{argv}");
        }
    }

    // 2026-10-26 (V430): `pytest -c pytest.ini` and `unittest -p test_*.py` were refused as inline
    // code and shell syntax although argv is exec'd directly and `-c` follows `-m pytest`.
    #[test]
    fn verifier_admits_module_options_and_literal_operands_but_not_inline_code_or_shell_syntax() {
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let python = value["verifier_commands"][0]["argv"][0].clone();
        let with = |argv: serde_json::Value| {
            let mut changed = value.clone();
            changed["verifier_commands"][0]["argv"] = argv;
            changed["verifier_commands"][0]["pinned_files"] = json!([]);
            JSpaceMatcher::from_value(workspace.path(), &reseal(changed))
        };
        for argv in [
            json!([&python, "-B", "-m", "pytest", "-q", "-c", "pytest.ini", "--rootdir=."]),
            json!([&python, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]),
            json!([&python, "-B", "-m", "pytest", "-q", "src/test.rs::case[a-b]", "-k", "(a or b) and not {c}"]),
        ] {
            with(argv.clone()).unwrap_or_else(|error| panic!("{argv}: {}", error.code()));
        }
        for argv in [
            json!([&python, "-c", "pass"]),
            json!([&python, "-Bc", "pass"]),
            json!([&python, "-W", "ignore", "-c", "pass"]),
            json!([&python, "-Wignore", "-c", "pass"]),
            json!([&python, "-B", "-m", "pytest", "-q", "tests;rm"]),
            json!([&python, "-B", "-m", "pytest", "-q", "$HOME"]),
        ] {
            assert_eq!(with(argv.clone()).unwrap_err().code(), "JSPACE_VERIFIER_GRANT_MALFORMED", "{argv}");
        }
    }

    #[test]
    fn python_import_roots_reject_malformed_bounds_and_paths() {
        let (workspace, artifacts, value) = python_verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let src = root.join("src");
        fs::create_dir_all(root.join(".hidden/pkg")).unwrap();
        fs::create_dir_all(root.join(".git/pkg")).unwrap();
        fs::create_dir(root.join("colon:root")).unwrap();
        let cases = vec![
            json!(null), json!(false), json!("src"), json!({"root":src}), json!([1]),
            json!([]), json!(["src"]), json!([""]), json!([src, src]),
            json!([src, src, src, src, src]), json!([root.join("missing")]),
            json!([root.join("src/test.rs")]), json!([root.join(".git")]),
            json!([root.join(".git/pkg")]), json!([root.join("colon:root")]),
            json!([format!("{}/src/.", root.display())]),
            json!([format!("{}/src/../src", root.display())]),
            json!([format!("{}//src", root.display())]),
            json!([format!("{}/src/", root.display())]), json!(["x".repeat(1025)]),
        ];
        for roots in cases {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["python_import_roots"] = roots.clone();
            assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err(), "{roots}");
        }
        // Workspace root, hidden virtualenv-style directories and directories outside the
        // workspace are search-path context for a globally reading verifier, not grants.
        for roots in [
            json!([root]), json!([fs::canonicalize(artifacts.path()).unwrap()]),
            json!([root.join(".hidden"), root.join(".hidden/pkg")]),
        ] {
            let mut valid = value.clone();
            valid["verifier_commands"][0]["python_import_roots"] = roots.clone();
            let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(valid)).expect("admitted root");
            assert_eq!(serde_json::to_value(&matcher.verifier_commands()[0].python_import_roots).unwrap(), roots);
        }
    }

    #[test]
    fn verifier_cwd_is_optional_canonical_and_preserves_the_legacy_shape() {
        // V318: the starting directory grants nothing, so a worker may run tests from the
        // directory its acceptance harness uses, inside or outside the workspace.
        let (workspace, artifacts, value) = verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let legacy = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).unwrap();
        assert!(legacy.verifier_commands()[0].cwd.is_none());
        assert!(serde_json::to_value(&legacy.verifier_commands()[0]).unwrap().get("cwd").is_none());
        fs::create_dir_all(root.join(".git/pkg")).unwrap();
        fs::create_dir(root.join("colon:dir")).unwrap();
        std::os::unix::fs::symlink(root.join("src"), root.join("linked")).unwrap();
        for cwd in [
            json!(null), json!(false), json!("src"), json!([root]), json!(""),
            json!(root.join("missing")), json!(root.join(".git")), json!(root.join(".git/pkg")),
            json!(root.join("colon:dir")), json!(root.join("linked")),
            json!(format!("{}/src/.", root.display())), json!(format!("{}//src", root.display())),
            json!(format!("{}/src/", root.display())), json!("/".to_string() + &"x".repeat(1025)),
        ] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["cwd"] = cwd.clone();
            assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err(), "{cwd}");
        }
        fs::create_dir_all(value["verifier_commands"][0]["scratch_root"].as_str().unwrap()).unwrap();
        for cwd in [root.clone(), root.join("src"), fs::canonicalize(artifacts.path()).unwrap()] {
            let mut valid = value.clone();
            valid["verifier_commands"][0]["cwd"] = json!(cwd);
            let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(valid)).expect("admitted cwd");
            assert_eq!(matcher.verifier_commands()[0].cwd.as_deref(), Some(cwd.as_path()));
            matcher.revalidate_verifier_paths().unwrap();
        }
    }

    #[test]
    fn python_import_roots_need_no_read_scope_and_reject_symlink_chains() {
        use std::os::unix::fs::symlink;
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let imports = root.join("imports");
        fs::create_dir(&imports).unwrap();
        let mut ungranted = value.clone();
        ungranted["verifier_commands"][0]["python_import_roots"] = json!([imports]);
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(ungranted.clone())).unwrap();
        assert_eq!(matcher.check_path("read", &imports.join("module.py")).unwrap_err().code(),
            JSPACE_EXPANSION_REQUIRED, "an import root grants no tool read");

        fs::create_dir(root.join("src/pkg")).unwrap();
        symlink(root.join("src"), root.join("alias")).unwrap();
        for path in [root.join("alias"), root.join("alias/pkg")] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["python_import_roots"] = json!([path]);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).unwrap_err().code(),
                "JSPACE_VERIFIER_SYMLINK_DENIED");
        }
    }

    #[test]
    fn python_import_roots_accept_four_distinct_roots_and_revalidate_root_drift() {
        let (workspace, _artifacts, mut value) = python_verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        for name in ["one", "two", "three"] {
            fs::create_dir(root.join(name)).unwrap();
            fs::write(root.join(name).join("module.py"), "module").unwrap();
        }
        value["read_scopes"] = json!(["src/test.rs", "one/module.py", "two/**", "three"]);
        value["verifier_commands"][0]["python_import_roots"] =
            json!([root.join("src"), root.join("one"), root.join("two"), root.join("three")]);
        let mut matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value)).unwrap();
        fs::create_dir(&matcher.verifier_commands()[0].scratch_root).unwrap();
        matcher.revalidate_verifier_paths().unwrap();
        // Read-scope drift no longer invalidates search-path context; a vanished root does.
        matcher.scope_projection.read_scopes.retain(|scope| scope != "three");
        matcher.revalidate_verifier_paths().unwrap();
        fs::rename(root.join("one"), root.join("moved")).unwrap();
        assert!(matcher.revalidate_verifier_paths().is_err());
    }

    #[test]
    fn python_import_roots_revalidate_missing_and_symlinked_root_drift() {
        use std::os::unix::fs::symlink;
        let (workspace, _artifacts, mut value) = python_verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let imports = root.join("imports");
        fs::create_dir(&imports).unwrap();
        fs::write(imports.join("module.py"), "module").unwrap();
        value["read_scopes"] = json!(["src/test.rs", "imports/module.py"]);
        value["verifier_commands"][0]["python_import_roots"] = json!([imports]);
        let matcher = JSpaceMatcher::from_value(workspace.path(), &reseal(value)).unwrap();
        fs::create_dir(&matcher.verifier_commands()[0].scratch_root).unwrap();
        matcher.revalidate_verifier_paths().unwrap();
        let moved = root.join("moved");
        fs::rename(&imports, &moved).unwrap();
        assert!(matcher.revalidate_verifier_paths().is_err());
        symlink(&moved, &imports).unwrap();
        assert_eq!(matcher.revalidate_verifier_paths().unwrap_err().code(), "JSPACE_VERIFIER_SYMLINK_DENIED");
    }

    #[test]
    fn python_import_roots_require_cpython_names_and_environment_aware_flags() {
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let root = fs::canonicalize(workspace.path()).unwrap();
        let source = value["verifier_commands"][0]["argv"][0].as_str().unwrap();
        for name in ["python", "python3", "node", "pypy3", "python3-config", "pythonw", "Python3", "python3x"] {
            let executable = root.join(name);
            fs::copy(source, &executable).unwrap();
            let mut changed = value.clone();
            changed["verifier_commands"][0]["argv"][0] = json!(executable);
            assert_eq!(JSpaceMatcher::from_value(workspace.path(), &reseal(changed)).is_ok(),
                matches!(name, "python" | "python3"), "{name}");
        }
        for flag in ["-I", "-E", "-IE", "-sE", "-BI"] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"].as_array_mut().unwrap().insert(1, json!(flag));
            assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err(), "{flag}");
        }
        for flag in ["-B", "-Werror::ImportWarning", "-Xfoo=Environment"] {
            let mut aware = value.clone();
            aware["verifier_commands"][0]["argv"].as_array_mut().unwrap().insert(1, json!(flag));
            JSpaceMatcher::from_value(workspace.path(), &reseal(aware)).unwrap();
        }
    }

    #[test]
    fn python_import_roots_raw_field_is_authorization_bound() {
        let (workspace, _artifacts, value) = python_verifier_fixture();
        let original = authorization_semantic_sha256(&value).unwrap();
        for roots in [json!([]), json!(null), json!(["/different/root"])] {
            let mut changed = value.clone();
            changed["verifier_commands"][0]["python_import_roots"] = roots;
            assert_ne!(authorization_semantic_sha256(&changed).unwrap(), original);
            changed["content_sha256"] = json!(semantic_sha256(&changed));
            assert!(JSpaceMatcher::from_value(workspace.path(), &changed).is_err());
        }
        let mut removed = value;
        removed["verifier_commands"][0].as_object_mut().unwrap().remove("python_import_roots");
        assert_ne!(authorization_semantic_sha256(&removed).unwrap(), original);
        removed["content_sha256"] = json!(semantic_sha256(&removed));
        assert!(JSpaceMatcher::from_value(workspace.path(), &removed).is_err());
    }

    #[test]
    fn verifier_grant_is_exact_and_coexists_with_source_read_without_shell_grant() {
        let (workspace, _artifacts, value) = verifier_fixture();
        let baseline = contract(workspace.path());
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &baseline)
                .unwrap()
                .verifier_commands()
                .len(),
            0
        );
        let admitted = JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone()))
            .expect("verifier grant");
        assert_ne!(
            admitted.authorization_semantic_sha256(),
            baseline["authorization_semantic_sha256"].as_str().unwrap()
        );
        assert_eq!(admitted.verifier_commands().len(), 1);
        assert_eq!(
            admitted.verifier_artifact_root().unwrap().to_str(),
            value["verifier_artifact_root"].as_str()
        );
        assert_eq!(
            admitted.check_verifier_command(0).unwrap().timeout_seconds,
            30
        );
        assert_eq!(
            admitted.check_verifier_command(1).unwrap_err().code(),
            "JSPACE_VERIFIER_NOT_GRANTED"
        );
        let pinned = value["verifier_commands"][0]["pinned_files"][0]["path"]
            .as_str()
            .unwrap();
        admitted
            .check_source_read(std::path::Path::new(pinned))
            .expect("source read retained");
        assert_eq!(
            admitted
                .check_command("shell_command", "git status --short")
                .unwrap_err()
                .code(),
            "JSPACE_COMMAND_DENIED"
        );
        let scratch = std::path::Path::new(
            value["verifier_commands"][0]["scratch_root"]
                .as_str()
                .unwrap(),
        );
        assert!(admitted.revalidate_verifier_paths().is_err());
        fs::create_dir(scratch).expect("scratch created by caller");
        admitted.revalidate_verifier_paths().expect("paths ready");
        let old = authorization_semantic_sha256(&baseline).unwrap();
        assert_eq!(
            old,
            baseline["authorization_semantic_sha256"].as_str().unwrap()
        );
        assert_eq!(old, authorization_semantic_sha256(&baseline).unwrap());
    }

    #[test]
    fn verifier_fields_are_paired_versioned_and_digest_bound() {
        let (workspace, _artifacts, value) = verifier_fixture();
        for field in ["verifier_commands", "verifier_artifact_root"] {
            let mut incomplete = value.clone();
            incomplete.as_object_mut().unwrap().remove(field);
            assert_eq!(
                JSpaceMatcher::from_value(workspace.path(), &reseal(incomplete))
                    .unwrap_err()
                    .code(),
                "JSPACE_VERIFIER_GRANT_MALFORMED"
            );
        }
        let (topology, worktree, metadata) = effect_topology();
        let mut unsupported = effect_contract(topology.path(), &worktree, &metadata);
        unsupported["verifier_commands"] = value["verifier_commands"].clone();
        unsupported["verifier_artifact_root"] = value["verifier_artifact_root"].clone();
        assert_eq!(
            JSpaceMatcher::from_value(topology.path(), &unsupported)
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_GRANT_UNSUPPORTED"
        );
        let sealed = reseal(value.clone());
        let mut changed = sealed.clone();
        changed["verifier_commands"][0]["timeout_seconds"] = json!(20);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &changed)
                .unwrap_err()
                .code(),
            "JSPACE_AUTHORIZATION_DIGEST_MISMATCH"
        );
        let mut changed = sealed;
        changed["verifier_artifact_root"] = json!("/elsewhere");
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &changed)
                .unwrap_err()
                .code(),
            "JSPACE_AUTHORIZATION_DIGEST_MISMATCH"
        );
    }

    #[test]
    fn verifier_rejects_network_bounds_shell_and_pinned_scope() {
        use sha2::Digest;
        let (workspace, _artifacts, value) = verifier_fixture();
        let mut invalid = value.clone();
        invalid["denied_operations"] = json!(["install", "system_mutation"]);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_OPERATION_DENIED"
        );
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["network"] = json!(true);
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["timeout_seconds"] = json!(301);
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let long_arg = "x".repeat(1025);
        for arg in ["arg;whoami", "", long_arg.as_str()] {
            let mut invalid = value.clone();
            invalid["verifier_commands"][0]["argv"]
                .as_array_mut()
                .unwrap()
                .push(json!(arg));
            assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        }
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["argv"] = json!([]);
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["argv"]
            .as_array_mut()
            .unwrap()
            .extend((0..31).map(|_| json!("safe")));
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["argv"][0] = json!("/bin/sh");
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["pinned_files"][0]["path"] =
            json!(_artifacts.path().join("elsewhere"));
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let outside = std::path::Path::new(value["verifier_artifact_root"].as_str().unwrap())
            .join("outside.rs");
        fs::write(&outside, "outside").expect("outside entry");
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(outside);
        invalid["verifier_commands"][0]["pinned_files"][0]["sha256"] = json!(format!(
            "{:x}",
            sha2::Sha256::digest(fs::read(&outside).unwrap())
        ));
        invalid["verifier_commands"][0]["argv"][1] = json!(outside);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_PATH_OUTSIDE_ROOT"
        );
        let mut invalid = value.clone();
        invalid["read_scopes"] = json!(["src/other.rs"]);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            JSPACE_EXPANSION_REQUIRED
        );
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["scratch_root"] = json!(workspace.path().join("scratch"));
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["scratch_root"] = value["verifier_artifact_root"].clone();
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
        let mut invalid = value.clone();
        invalid["verifier_commands"]
            .as_array_mut()
            .unwrap()
            .push(value["verifier_commands"][0].clone());
        assert!(JSpaceMatcher::from_value(workspace.path(), &reseal(invalid)).is_err());
    }

    #[test]
    fn verifier_rejects_stale_files_and_symlink_traversal() {
        use std::os::unix::fs::symlink;
        let (workspace, _artifacts, value) = verifier_fixture();
        let matcher =
            JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone())).expect("admitted");
        let pinned = matcher.verifier_commands()[0].pinned_files[0].path.clone();
        fs::write(&pinned, "changed").expect("stale file");
        assert_eq!(
            matcher.revalidate_verifier_paths().unwrap_err().code(),
            "NOKIY_LOCAL_CONTEXT_STALE"
        );
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(value.clone()))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_FILE_MISMATCH"
        );
        fs::write(&pinned, "test entry").expect("restore pin");
        let executable =
            std::path::PathBuf::from(value["verifier_commands"][0]["argv"][0].as_str().unwrap());
        let executable_bytes = fs::read(&executable).expect("binary");
        let mut stale_executable = executable_bytes.clone();
        *stale_executable.last_mut().unwrap() ^= 1;
        fs::write(&executable, stale_executable).expect("stale executable");
        assert_eq!(
            matcher.revalidate_verifier_paths().unwrap_err().code(),
            "NOKIY_LOCAL_CONTEXT_STALE"
        );
        fs::write(&executable, executable_bytes).expect("restore executable");
        let mut invalid = value.clone();
        let link =
            std::path::Path::new(value["verifier_artifact_root"].as_str().unwrap()).join("link");
        symlink(workspace.path().join("src"), &link).expect("symlink");
        invalid["verifier_commands"][0]["scratch_root"] = json!(link.join("scratch"));
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_SYMLINK_DENIED"
        );
        let pinned_link = workspace.path().join("src/link.rs");
        symlink(&pinned, &pinned_link).expect("pinned link");
        let mut invalid = value;
        invalid["verifier_commands"][0]["argv"][1] = json!(pinned_link);
        invalid["verifier_commands"][0]["pinned_files"][0]["path"] = json!(pinned_link);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_SYMLINK_DENIED"
        );
    }

    #[test]
    fn verifier_rejects_inline_interpreter_and_duplicate_scratch() {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::PermissionsExt;
        let (workspace, _artifacts, value) = verifier_fixture();
        let node = workspace.path().join("node");
        fs::write(&node, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).expect("node fixture");
        fs::set_permissions(&node, fs::Permissions::from_mode(0o755)).expect("node executable");
        let mut invalid = value.clone();
        invalid["verifier_commands"][0]["argv"][0] = json!(node);
        invalid["verifier_commands"][0]["executable_sha256"] =
            json!(format!("{:x}", Sha256::digest(fs::read(&node).unwrap())));
        invalid["verifier_commands"][0]["argv"]
            .as_array_mut()
            .unwrap()
            .push(json!("--eval"));
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_GRANT_MALFORMED"
        );

        let mut invalid = value.clone();
        let mut second = value["verifier_commands"][0].clone();
        second["argv"].as_array_mut().unwrap().push(json!("safe"));
        invalid["verifier_commands"]
            .as_array_mut()
            .unwrap()
            .push(second);
        assert_eq!(
            JSpaceMatcher::from_value(workspace.path(), &reseal(invalid))
                .unwrap_err()
                .code(),
            "JSPACE_VERIFIER_SCRATCH_INVALID"
        );
    }

    #[test]
    fn source_read_requires_an_exact_grant_without_changing_shell_or_patch_metadata() {
        let root = tempfile::tempdir().expect("workspace");
        fs::create_dir(root.path().join("src")).expect("source directory");
        fs::write(root.path().join("src/main.rs"), "fn main() {}\n").expect("source file");
        let mut value = contract(root.path());
        value["read_scopes"] = json!(["src/main.rs"]);
        value["write_scopes"] = json!(["src/main.rs"]);
        value["command_templates"] = json!([]);
        value["source_read"] = json!(true);
        let admitted = JSpaceMatcher::from_value(root.path(), &reseal(value.clone()))
            .expect("source read grant");
        assert!(admitted.source_read_enabled());
        assert!(
            admitted
                .check_source_read(&root.path().join("src/main.rs"))
                .is_ok()
        );
        assert_eq!(
            admitted
                .check_source_read(&root.path().join("src/other.rs"))
                .unwrap_err()
                .code(),
            JSPACE_EXPANSION_REQUIRED
        );
        assert_eq!(
            admitted
                .check_command("bash", "cat src/main.rs")
                .unwrap_err()
                .code(),
            "JSPACE_COMMAND_DENIED"
        );
        assert!(admitted.check_command("apply_patch", "").is_ok());
        assert!(admitted.check_command("task_status", "done").is_ok());
        assert!(admitted.check_command("planning", "[]").is_ok());

        value["declared_targets"] = json!([]);
        let no_patch_target = JSpaceMatcher::from_value(root.path(), &reseal(value.clone()))
            .expect("read grant without patch target");
        assert_eq!(
            no_patch_target
                .check_command("apply_patch", "")
                .unwrap_err()
                .code(),
            "JSPACE_COMMAND_DENIED"
        );

        value["read_scopes"] = json!(["src/**"]);
        assert_eq!(
            JSpaceMatcher::from_value(root.path(), &reseal(value))
                .unwrap_err()
                .code(),
            "JSPACE_SOURCE_READ_DENIED"
        );
    }

    #[cfg(unix)]
    fn mapped_source_read_fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf, Value) {
        use crate::mapped_source_read::MappedSourceReadBinding;
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::symlink;
        let tree = tempfile::tempdir().expect("mapped fixture");
        let root = tree.path().canonicalize().expect("canonical fixture");
        let repo = root.join("repo");
        let physical = root.join("external/dcf/v2");
        fs::create_dir_all(repo.join("config/contracts")).expect("contract directory");
        fs::create_dir_all(repo.join("output")).expect("logical parent");
        fs::create_dir_all(&physical).expect("physical root");
        fs::write(physical.join("current.json"), "{\"generation\":1}\n").expect("pointer");
        fs::write(physical.join("other.json"), "not admitted\n").expect("other pointer");
        symlink(physical.parent().unwrap(), repo.join("output/dcf_runtime")).expect("alias");
        let logical = "output/dcf_runtime/v2/current.json";
        let bytes = serde_json::to_vec(&json!({
            "schema_version":"dcf_v2_contract_v1", "status":"active",
            "authority_effect":"none", "no_apply":true,
            "runtime":{"namespace":"output/dcf_runtime/v2", "current_pointer":logical},
            "two_tier_storage_cutover":{
                "logical_runtime_namespace":"output/dcf_runtime/v2",
                "target_physical_runtime_root":physical,
            }
        })).expect("DCF contract");
        fs::write(repo.join("config/contracts/dcf_v2_contract.json"), &bytes).expect("contract");
        let binding = MappedSourceReadBinding::derive(
            &repo, &bytes, &format!("{:x}", Sha256::digest(&bytes)), logical,
        ).expect("binding");
        let mut value = contract(&repo);
        value["read_scopes"] = json!([logical]);
        value["write_scopes"] = json!([logical]);
        value["declared_targets"] = json!([logical]);
        value["command_templates"] = json!([]);
        value["source_read"] = json!(true);
        value["mapped_source_reads"] = json!([binding]);
        (tree, repo, physical, reseal(value))
    }

    #[test]
    #[cfg(unix)]
    fn mapped_source_read_admits_only_the_exact_logical_source_read() {
        let (_tree, repo, physical, value) = mapped_source_read_fixture();
        let matcher = JSpaceMatcher::from_value(&repo, &value).expect("mapped admission");
        let logical = std::path::Path::new("output/dcf_runtime/v2/current.json");
        let absolute_logical = repo.join(logical);
        let physical_target = physical.join("current.json");
        assert_eq!(matcher.mapped_source_reads().len(), 1);
        matcher.check_source_read(logical).expect("exact mapped source read");
        for target in [
            std::path::Path::new("output/dcf_runtime/v2/other.json"),
            std::path::Path::new("output/./dcf_runtime/v2/current.json"),
            std::path::Path::new("output/dcf_runtime/v2/../v2/current.json"),
            absolute_logical.as_path(),
            physical_target.as_path(),
        ] {
            assert!(matcher.check_source_read(target).is_err(), "{}", target.display());
        }
        assert!(matcher.check_path("read", logical).is_err());
        assert!(matcher.check_path("modify", logical).is_err());
        assert!(matcher.check_command("bash", "cat output/dcf_runtime/v2/current.json").is_err());
        assert!(matcher.check_command("cat", "output/dcf_runtime/v2/current.json").is_err());

        let mut ordinary = value.clone();
        ordinary.as_object_mut().unwrap().remove("mapped_source_reads");
        let ordinary = JSpaceMatcher::from_value(&repo, &reseal(ordinary)).expect("ordinary admission");
        assert!(ordinary.mapped_source_reads().is_empty());
        assert!(ordinary.check_source_read(logical).is_err());
    }

    #[test]
    #[cfg(unix)]
    fn mapped_source_read_grant_is_authorization_bound_and_fails_closed() {
        let (_tree, repo, _physical, value) = mapped_source_read_fixture();
        let digest = authorization_semantic_sha256(&value).expect("mapped digest");
        let mut without = value.clone();
        without.as_object_mut().unwrap().remove("mapped_source_reads");
        let without_digest = authorization_semantic_sha256(&without).expect("ordinary digest");
        assert_ne!(digest, without_digest);
        assert_eq!(authorization_semantic_sha256(&reseal(without)).unwrap(), without_digest);

        for scenario in 0..12 {
            let mut invalid = value.clone();
            match scenario {
                0 => invalid["schema_version"] = json!("jspace_contract_v1"),
                1 => invalid["schema_version"] = json!("jspace_contract_v3"),
                2 => invalid["source_read"] = json!(false),
                3 => { invalid.as_object_mut().unwrap().remove("source_read"); },
                4 => invalid["mapped_source_reads"] = json!([]),
                5 => invalid["mapped_source_reads"] = json!([value["mapped_source_reads"][0], value["mapped_source_reads"][0]]),
                6 => invalid["mapped_source_reads"] = Value::Null,
                7 => invalid["mapped_source_reads"][0]["ignored_unknown"] = json!(true),
                8 => invalid["read_scopes"] = json!(["output/dcf_runtime/v2/**"]),
                9 => invalid["allowed_operations"] = json!(["command"]),
                10 => invalid["mapped_source_reads"][0]["requested_logical_path"] = json!(repo.join("output/dcf_runtime/v2/current.json")),
                11 => invalid["mapped_source_reads"][0]["requested_logical_path"] = json!("output/./dcf_runtime/v2/current.json"),
                _ => unreachable!(),
            }
            assert!(authorization_semantic_sha256(&invalid).is_err(), "scenario {scenario}");
            assert!(JSpaceMatcher::from_value(&repo, &invalid).is_err(), "scenario {scenario}");
        }

        let mut tampered = value.clone();
        tampered["mapped_source_reads"][0]["contract_identity"]["inode"] = json!(0);
        assert_ne!(authorization_semantic_sha256(&tampered).unwrap(), digest);
        assert!(JSpaceMatcher::from_value(&repo, &tampered).is_err());
        assert_eq!(
            JSpaceMatcher::from_value(&repo, &reseal(tampered)).unwrap_err().code(),
            "JSPACE_MAPPED_SOURCE_READ_BINDING_INVALID",
        );
    }

    #[test]
    #[cfg(unix)]
    fn mapped_source_read_revalidates_a_cached_binding_after_contract_change() {
        let (_tree, repo, _physical, value) = mapped_source_read_fixture();
        let cache = JSpaceAdmissionCache::default();
        let matcher = cache.admit("mapped", &repo, Some(&value)).unwrap().unwrap();
        fs::write(repo.join("config/contracts/dcf_v2_contract.json"), "{}\n").expect("change DCF contract");
        let cached = cache.admit("mapped", &repo, Some(&value)).unwrap().unwrap();
        assert_eq!(cache.admissions(), 1);
        assert!(std::sync::Arc::ptr_eq(&matcher, &cached));
        assert_eq!(
            cached.check_source_read(std::path::Path::new("output/dcf_runtime/v2/current.json")).unwrap_err().code(),
            "JSPACE_MAPPED_SOURCE_READ_BINDING_INVALID",
        );
    }

    #[test]
    fn source_read_v3_requires_an_exact_effect_domain_scope() {
        let root = tempfile::tempdir().expect("workspace");
        let root_path = fs::canonicalize(root.path()).expect("canonical workspace");
        fs::create_dir(root.path().join("src")).expect("source directory");
        fs::write(root.path().join("src/main.rs"), "fn main() {}\n").expect("source file");
        let mut value = contract(&root_path);
        value["schema_version"] = json!("jspace_contract_v3");
        value["dcf_generation"]["context_mode"] = json!("local_workspace_jspace");
        value["dcf_generation"]["dcf_available"] = json!(false);
        value.as_object_mut().expect("object").remove("read_scopes");
        value
            .as_object_mut()
            .expect("object")
            .remove("write_scopes");
        value
            .as_object_mut()
            .expect("object")
            .remove("declared_targets");
        value["allowed_operations"] = json!(["command"]);
        value["command_templates"] = json!([]);
        value["effect_capabilities"] = json!([{
            "domain_id": "workspace", "root": root_path,
            "read_scopes": ["src/main.rs", "src/**"], "write_scopes": [],
            "allowed_operations": ["read"], "declared_targets": []
        }]);
        value["source_read"] = json!(true);
        let admitted =
            JSpaceMatcher::from_value(&root_path, &reseal(value)).expect("v3 source read grant");
        assert!(
            admitted
                .check_source_read(&root_path.join("src/main.rs"))
                .is_ok()
        );
        assert_eq!(
            admitted
                .check_source_read(&root_path.join("src/other.rs"))
                .unwrap_err()
                .code(),
            JSPACE_EXPANSION_REQUIRED
        );
    }

    #[test]
    fn authorization_digest_matches_dcf_cross_language_vector() {
        let value = json!({
            "repo_root": "/workspace",
            "dcf_generation": {
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-a"}
                    }
                }
            },
            "matched_surface_ids": ["surface-test"],
            "read_scopes": ["src/**"],
            "write_scopes": ["src/**"],
            "allowed_operations": ["read", "create", "modify", "command"],
            "denied_operations": ["network", "install", "system_mutation"],
            "command_templates": [{
                "argv": ["git", "status", "--short"],
                "effects": ["read"],
                "targets": []
            }],
            "declared_targets": ["src/main.rs"],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            }
        });

        assert_eq!(
            authorization_semantic_sha256(&value).expect("authorization digest"),
            AUTHORIZATION_DIGEST_CROSS_LANGUAGE_VECTOR
        );
        let mut canonical = value.clone();
        canonical["command_effect_policy"] = json!("trusted_argv_effects_v1");
        assert_eq!(
            authorization_semantic_sha256(&canonical).expect("canonical DCF digest"),
            "8cf66552c66859c567c15e09c5c60e1cc99277a5634b9f1b634205bbfcd2ef31"
        );
    }

    #[test]
    fn v2_rejects_unknown_policy_and_policy_removal() {
        let root = tempfile::tempdir().expect("root");
        let mut value = contract(root.path());
        for policy in [json!("unrestricted"), Value::Null, json!({}), json!(1)] {
            value["command_effect_policy"] = policy;
            assert!(authorization_semantic_sha256(&value).is_err());
        }
        value["command_effect_policy"] = json!("trusted_argv_effects_v1");
        let canonical = reseal(value);
        assert!(
            super::verified_contract_digests(
                &canonical,
                canonical.as_object().unwrap(),
                "jspace_contract_v2"
            )
            .is_ok()
        );
        let mut stripped = canonical;
        stripped
            .as_object_mut()
            .unwrap()
            .remove("command_effect_policy");
        stripped.as_object_mut().unwrap().remove("content_sha256");
        stripped["content_sha256"] = json!(semantic_sha256(&stripped));
        assert_eq!(
            super::verified_contract_digests(
                &stripped,
                stripped.as_object().unwrap(),
                "jspace_contract_v2"
            )
            .unwrap_err()
            .code(),
            "JSPACE_AUTHORIZATION_DIGEST_MISMATCH"
        );
    }

    #[test]
    fn v2_cat_checks_exact_argv_and_every_read_target() {
        let root = tempfile::tempdir().expect("root");
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/a.txt"), "a").unwrap();
        fs::write(root.path().join("secret.txt"), "secret").unwrap();
        let mut value = contract(root.path());
        value["command_effect_policy"] = json!("trusted_argv_effects_v1");
        value["command_templates"] = json!([{
            "argv": ["cat", "src/a.txt"], "effects": ["read"],
            "targets": [{"operation": "read", "path": "src/a.txt", "argv_index": 1}]
        }]);
        let admitted = JSpaceMatcher::from_value(root.path(), &reseal(value.clone())).unwrap();
        assert!(admitted.check_command("bash", "cat src/a.txt").is_ok());
        assert!(admitted.check_command("bash", "cat secret.txt").is_err());
        assert!(
            admitted
                .check_command("bash", "cat src/a.txt; pwd")
                .is_err()
        );
        assert!(
            admitted
                .check_path("modify", &root.path().join("src/a.txt"))
                .is_err()
        );
        value["command_templates"][0]["argv"][1] = json!("secret.txt");
        value["command_templates"][0]["targets"][0]["path"] = json!("secret.txt");
        let outside = JSpaceMatcher::from_value(root.path(), &reseal(value)).unwrap();
        assert!(outside.check_command("bash", "cat secret.txt").is_err());
    }

    #[test]
    fn exact_argv_index_preserves_denials_and_first_template_order() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/ok.txt"), "ok").unwrap();
        fs::write(root.path().join("outside.txt"), "outside").unwrap();
        let mut value = contract(root.path());
        value["command_templates"] = json!([{
            "argv": ["cat", "src/ok.txt"], "effects": ["read"],
            "targets": [{"operation": "read", "path": "src/ok.txt", "argv_index": 1}]
        }]);
        let matcher = JSpaceMatcher::from_value(root.path(), &reseal(value.clone())).unwrap();
        assert_eq!(matcher.command_template_index.get(&vec!["cat".into(), "src/ok.txt".into()]), Some(&0));
        assert!(matcher.check_command("bash", "cat src/ok.txt").is_ok());
        assert_eq!(matcher.check_command("bash", "cat src/missing.txt").unwrap_err().code(),
                   "JSPACE_COMMAND_DENIED");
        assert_eq!(matcher.check_command("bash", "cat src/ok.txt; pwd").unwrap_err().code(),
                   "JSPACE_COMMAND_SYNTAX_UNSAFE");
        assert_eq!(matcher.check_command("unknown", "cat src/ok.txt").unwrap_err().code(),
                   "JSPACE_UNKNOWN_TOOL");

        // Duplicate argv is rejected at admission, never silently replaced by the index.
        let duplicate = value["command_templates"][0].clone();
        value["command_templates"].as_array_mut().unwrap().push(duplicate);
        assert_eq!(JSpaceMatcher::from_value(root.path(), &reseal(value)).unwrap_err().code(),
                   "JSPACE_COMMAND_TEMPLATE_DUPLICATE");

        // The index's first-entry behavior also holds if a legacy matcher contains repeats.
        let mut repeated = matcher.clone();
        repeated.command_templates.push(repeated.command_templates[0].clone());
        repeated.command_templates[1].targets[0].path = "outside.txt".into();
        assert!(repeated.check_command("bash", "cat src/ok.txt").is_ok());
        repeated.command_templates[0].targets[0].path = "outside.txt".into();
        assert_eq!(repeated.check_command("bash", "cat src/ok.txt").unwrap_err().code(),
                   JSPACE_EXPANSION_REQUIRED);
        repeated.command_templates[0].targets[0].path = "src/ok.txt".into();
        repeated.command_templates[0].effects = vec!["network".into()];
        assert_eq!(repeated.check_command("bash", "cat src/ok.txt").unwrap_err().code(),
                   "JSPACE_OPERATION_DENIED");
    }

    #[test]
    #[ignore = "deterministic opt-in local timing; not a performance gate"]
    fn bench_exact_argv_index_vs_linear_reference() {
        use std::hint::black_box;
        use std::time::Instant;
        let root = tempfile::tempdir().unwrap();
        for count in [16, 128, 1024] {
            let mut value = contract(root.path());
            value["command_templates"] = json!((0..count).map(|n| json!({
                "argv": ["git", "status", format!("--format-{n}")],
                "effects": ["read"], "targets": []
            })).collect::<Vec<_>>());
            let matcher = JSpaceMatcher::from_value(root.path(), &reseal(value)).unwrap();
            let argv = ["git".to_string(), "status".to_string(), format!("--format-{}", count - 1)];
            let command = argv.join(" ");
            let runs = 2000;
            let start = Instant::now();
            for _ in 0..runs {
                matcher.check_command("bash", black_box(&command)).unwrap();
                black_box(());
            }
            let indexed = start.elapsed();
            let start = Instant::now();
            for _ in 0..runs {
                let command_type = super::normalize_command_type("bash");
                matcher.check_operation("command", &command_type).unwrap();
                let (raw, workdir) = super::shell_command_parts(&command);
                assert!(workdir.is_none());
                let parsed = super::parse_shell_argv(&raw).unwrap();
                let template = matcher.command_templates.iter().find(|template| template.argv == parsed).unwrap();
                for effect in &template.effects {
                    matcher.check_operation(effect, &raw).unwrap();
                }
                black_box(template);
            }
            eprintln!("templates={count} indexed_check={indexed:?} linear_check={:?}", start.elapsed());
        }
    }

    fn local_directory_source_read_fixture() -> (tempfile::TempDir, std::path::PathBuf, Value) {
        use sha2::{Digest, Sha256};
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        fs::create_dir(root.join("src")).unwrap();
        fs::write(root.join("src/new.txt"), "needle\n").unwrap();
        fs::write(root.join("exact.txt"), "exact\n").unwrap();
        let executable = |name: &str| {
            let path = root.join(name);
            fs::write(&path, name).unwrap();
            json!({"path": path, "sha256": format!("{:x}", Sha256::digest(name.as_bytes()))})
        };
        let mut value = contract(&root);
        value["dcf_generation"]["context_mode"] = json!("local_workspace_jspace");
        value["dcf_generation"]["dcf_available"] = json!(false);
        value["dcf_generation"]["required_domain_bindings"] = json!({});
        value["read_commands"] =
            json!({"roots":["src"],"rg":executable("rg"),"cat":executable("cat")});
        value["source_read"] = json!(true);
        value["command_templates"] = json!([]);
        value["allowed_operations"] = json!(["read", "command"]);
        value["read_scopes"] = json!(["src/**"]);
        value["write_scopes"] = json!([]);
        value["declared_targets"] = json!([]);
        (temp, root, value)
    }

    #[test]
    fn pinned_search_classification_reuses_discovery_denials() {
        let (_temp, root, value) = local_directory_source_read_fixture();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        let rg = root.join("rg").display().to_string();
        let direct = format!("{rg} --no-config --max-filesize=1M -n -m 20 -- 'a|b' src");
        assert!(admitted.check_read_search_command("zsh", &direct).is_ok());
        for line in [
            "rg --no-config --max-filesize=1M -- a src".to_owned(),
            format!("{direct}; false"),
            format!("{direct} | cat"),
            format!("env {direct}"),
            format!("{rg} --no-config --max-filesize=1M -- $(echo a) src"),
            format!("{rg} --no-config --max-filesize=1M --pre cat -- a src"),
            format!("{rg} --no-config --max-filesize=1M --follow -- a src"),
            format!("{rg} --no-config --max-filesize=1M -- a ../outside"),
            json!({"command": direct, "cwd": root}).to_string(),
            format!("{} -- src/new.txt", root.join("cat").display()),
        ] {
            assert!(
                admitted.check_read_search_command("zsh", &line).is_err(),
                "{line}"
            );
        }
        assert!(admitted.check_read_search_command("apply_patch", &direct).is_err());
        std::fs::write(root.join("rg"), "replaced").unwrap();
        assert!(admitted.check_read_search_command("zsh", &direct).is_err());
    }

    #[test]
    fn local_directory_source_read_accepts_only_existing_cat_authority() {
        let (_temp, root, value) = local_directory_source_read_fixture();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        assert!(admitted.source_read_enabled());
        let cat = root.join("cat").display().to_string();
        for file in ["src/new.txt", "src/discovered-later.txt"] {
            fs::write(root.join(file), "needle\n").unwrap();
            assert!(!admitted.read_scopes.matches_exact(file));
            admitted.check_source_read(std::path::Path::new(file)).unwrap();
            admitted.check_source_read(&root.join(file)).unwrap();
            admitted.check_command("bash", &format!("{cat} -- {file}")).unwrap();
        }
        assert!(admitted.check_command("bash", "cat src/new.txt").is_err());
        assert!(admitted.check_command("apply_patch", "").is_err());
        assert!(admitted.check_path("modify", &root.join("src/new.txt")).is_err());
    }

    #[test]
    fn directory_source_read_does_not_depend_on_context_generation_labels() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        value["read_scopes"] = json!(["src/**", "exact.txt"]);
        for (field, replacement) in [
            ("context_mode", None),
            ("context_mode", Some(json!(null))),
            ("context_mode", Some(json!(false))),
            ("context_mode", Some(json!("dcf"))),
            ("dcf_available", None),
            ("dcf_available", Some(json!(null))),
            ("dcf_available", Some(json!("false"))),
            ("dcf_available", Some(json!(0))),
            ("dcf_available", Some(json!(true))),
        ] {
            let mut nonlocal = value.clone();
            if let Some(replacement) = replacement {
                nonlocal["dcf_generation"][field] = replacement;
            } else {
                nonlocal["dcf_generation"].as_object_mut().unwrap().remove(field);
            }
            let admitted = JSpaceMatcher::from_value(&root, &reseal(nonlocal.clone())).unwrap();
            admitted.check_source_read(&root.join("exact.txt")).unwrap();
            admitted.check_source_read(&root.join("src/new.txt")).unwrap();
            nonlocal["read_scopes"] = json!(["src/**"]);
            let admitted = JSpaceMatcher::from_value(&root, &reseal(nonlocal)).unwrap();
            admitted.check_source_read(&root.join("src/new.txt")).unwrap();
        }
        let sealed = reseal(value);
        for generation in [None, Some(json!(null)), Some(json!([]))] {
            let mut malformed = sealed.clone();
            if let Some(generation) = generation {
                malformed["dcf_generation"] = generation;
            } else {
                malformed.as_object_mut().unwrap().remove("dcf_generation");
            }
            malformed["content_sha256"] = json!(semantic_sha256(&malformed));
            assert_eq!(
                JSpaceMatcher::from_value(&root, &malformed).unwrap_err().code(),
                "JSPACE_CONTRACT_MALFORMED"
            );
        }
    }

    #[test]
    fn local_directory_source_read_requires_valid_policy_and_operation_grants() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        value["read_scopes"] = json!(["src/**", "exact.txt"]);
        let mut exact_only = value.clone();
        exact_only.as_object_mut().unwrap().remove("read_commands");
        let admitted = JSpaceMatcher::from_value(&root, &reseal(exact_only.clone())).unwrap();
        admitted.check_source_read(&root.join("exact.txt")).unwrap();
        assert_eq!(
            admitted.check_source_read(&root.join("src/new.txt")).unwrap_err().code(),
            JSPACE_EXPANSION_REQUIRED
        );
        exact_only["read_scopes"] = json!(["src/**"]);
        assert_eq!(
            JSpaceMatcher::from_value(&root, &reseal(exact_only)).unwrap_err().code(),
            "JSPACE_SOURCE_READ_DENIED"
        );

        let sealed = reseal(value.clone());
        for policy in [json!(null), json!({}), {
            let mut policy = value["read_commands"].clone();
            policy["cat"]["sha256"] = json!("invalid");
            policy
        }, {
            let mut policy = value["read_commands"].clone();
            policy["roots"] = json!(["src/*"]);
            policy
        }] {
            let mut malformed = sealed.clone();
            malformed["read_commands"] = policy;
            malformed["content_sha256"] = json!(semantic_sha256(&malformed));
            assert_eq!(
                JSpaceMatcher::from_value(&root, &malformed).unwrap_err().code(),
                "JSPACE_READ_COMMAND_DENIED"
            );
        }
        let mut disabled = value.clone();
        disabled.as_object_mut().unwrap().remove("source_read");
        let admitted = JSpaceMatcher::from_value(&root, &reseal(disabled)).unwrap();
        assert!(admitted.source_read_enabled());
        admitted.check_source_read(&root.join("src/new.txt")).unwrap();
        assert!(admitted.check_source_read(&root.join("exact.txt")).is_err());
        let mut invalid = sealed;
        invalid["source_read"] = json!(false);
        invalid["content_sha256"] = json!(semantic_sha256(&invalid));
        assert_eq!(
            JSpaceMatcher::from_value(&root, &invalid).unwrap_err().code(),
            "JSPACE_SOURCE_READ_GRANT_INVALID"
        );
        for operation in ["read", "command"] {
            let mut denied = value.clone();
            denied["denied_operations"] = json!([operation]);
            assert!(JSpaceMatcher::from_value(&root, &reseal(denied)).is_err());
            let mut absent = value.clone();
            absent["allowed_operations"] = json!([if operation == "read" { "command" } else { "read" }]);
            assert!(JSpaceMatcher::from_value(&root, &reseal(absent)).is_err());
        }
        let mut unsupported = value;
        unsupported["schema_version"] = json!("jspace_contract_v3");
        unsupported["allowed_operations"] = json!(["command"]);
        unsupported["effect_capabilities"] = json!([{
            "domain_id": "workspace", "root": root,
            "read_scopes": ["src/**", "exact.txt"], "write_scopes": [],
            "allowed_operations": ["read"], "declared_targets": []
        }]);
        for field in ["read_scopes", "write_scopes", "declared_targets"] {
            unsupported.as_object_mut().unwrap().remove(field);
        }
        assert!(JSpaceMatcher::from_value(&root, &reseal(unsupported)).is_err());
    }

    #[test]
    fn local_directory_source_read_keeps_cat_path_and_file_boundaries() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        fs::create_dir(root.join("other")).unwrap();
        fs::write(root.join("other/readable.txt"), "outside policy roots").unwrap();
        fs::create_dir(root.join("src/nested")).unwrap();
        fs::write(root.join("src/nested/file"), "nested").unwrap();
        fs::create_dir(root.join("src/.private")).unwrap();
        fs::write(root.join("src/.private/file"), "hidden").unwrap();
        fs::write(root.join("src/.hidden"), "hidden").unwrap();
        fs::File::create(root.join("src/large")).unwrap().set_len(1024 * 1024 + 1).unwrap();
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("file"), "outside workspace").unwrap();
        value["read_scopes"] = json!(["src/**", "other/**"]);
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        admitted.check_path("read", &root.join("other/readable.txt")).unwrap();
        admitted.check_source_read(&root.join("src/nested/file")).unwrap();
        let denied = |target: &std::path::Path| {
            assert!(admitted.check_source_read(target).is_err(), "{}", target.display());
            assert!(super::read_commands::check(
                &admitted,
                &[root.join("cat").to_str().unwrap().to_owned(), "--".to_owned(), target.to_str().unwrap().to_owned()],
                &root,
            ).is_err(), "{}", target.display());
        };
        for target in [
            root.join("other/readable.txt"),
            outside.path().join("file"),
            root.join("src/../exact.txt"),
            root.join("src/.hidden"),
            root.join("src/.private/file"),
            root.join("src/nested"),
            root.join("src/large"),
            root.join("src/missing"),
        ] {
            denied(&target);
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(root.join("src/new.txt"), root.join("src/link")).unwrap();
            std::os::unix::fs::symlink(root.join("src/nested"), root.join("src/link-dir")).unwrap();
            std::os::unix::fs::symlink(outside.path(), root.join("src/escape")).unwrap();
            denied(&root.join("src/link"));
            denied(&root.join("src/link-dir/file"));
            denied(&root.join("src/escape/file"));
        }
    }

    #[test]
    fn dcf_directory_reader_without_exact_grant_matches_cat_authority() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        value.as_object_mut().unwrap().remove("source_read");
        value["dcf_generation"].as_object_mut().unwrap().remove("context_mode");
        value["dcf_generation"].as_object_mut().unwrap().remove("dcf_available");
        value["read_scopes"] = json!(["src/**", "exact.txt"]);
        fs::write(root.join("src/.hidden"), "hidden").unwrap();
        fs::create_dir(root.join("src/nested")).unwrap();
        fs::write(root.join("src/nested/new.txt"), "nested").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(root.join("exact.txt"), root.join("src/link")).unwrap();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        for relative in ["src/new.txt", "src/nested/new.txt", "src/.hidden", "src/link",
                         "src/../exact.txt", "src/missing", "src/nested", "exact.txt"] {
            let target = root.join(relative);
            let cat = super::read_commands::check_file(&admitted, &target);
            let ranged = admitted.check_source_read(&target);
            assert_eq!(cat.is_ok(), ranged.is_ok(), "{relative}");
            assert_eq!(ranged.is_ok(), matches!(relative, "src/new.txt" | "src/nested/new.txt"));
        }
        fs::write(root.join("cat"), "changed cat").unwrap();
        assert!(admitted.check_source_read(&root.join("src/new.txt")).is_err());
    }

    #[test]
    fn local_directory_source_read_checks_pinned_cat_without_tightening_exact_reads() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        value["read_scopes"] = json!(["src/**", "exact.txt"]);
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        fs::write(root.join("rg"), "changed rg").unwrap();
        admitted.check_source_read(&root.join("src/new.txt")).unwrap();
        fs::write(root.join("cat"), "changed cat").unwrap();
        assert_eq!(
            admitted.check_source_read(&root.join("src/new.txt")).unwrap_err().code(),
            "JSPACE_READ_COMMAND_DENIED"
        );
        admitted.check_source_read(&root.join("exact.txt")).unwrap();
    }

    #[test]
    fn local_directory_source_read_mode_is_bound_to_fresh_admitted_content() {
        let (_temp, root, mut value) = local_directory_source_read_fixture();
        value["read_scopes"] = json!(["src/**", "exact.txt"]);
        let local = reseal(value);
        let cache = JSpaceAdmissionCache::default();
        let admitted = cache.admit("directory", &root, Some(&local)).unwrap().unwrap();
        admitted.check_source_read(&root.join("src/new.txt")).unwrap();
        let mut nonlocal = local.clone();
        nonlocal["dcf_generation"]["dcf_available"] = json!(true);
        assert_eq!(
            cache.admit("directory", &root, Some(&nonlocal)).unwrap_err().code(),
            "JSPACE_CONTENT_DIGEST_MISMATCH"
        );
        let nonlocal = reseal(nonlocal);
        assert_eq!(local["authorization_semantic_sha256"], nonlocal["authorization_semantic_sha256"]);
        assert_ne!(local["content_sha256"], nonlocal["content_sha256"]);
        let admitted = cache.admit("directory", &root, Some(&nonlocal)).unwrap().unwrap();
        admitted.check_source_read(&root.join("src/new.txt")).unwrap();
        admitted.check_source_read(&root.join("exact.txt")).unwrap();
    }

    #[test]
    fn discovery_accepts_dynamic_queries_and_reads_without_mutation_grants() {
        use sha2::{Digest, Sha256};
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        fs::create_dir(root.join("src")).unwrap();
        fs::write(root.join("src/new.txt"), "needle").unwrap();
        fs::write(root.join("src/.env"), "secret").unwrap();
        fs::write(root.join("outside.txt"), "outside").unwrap();
        let mut value = contract(&root);
        let executable = |name: &str| {
            let path = root.join(name);
            fs::write(&path, name).unwrap();
            json!({"path": path, "sha256": format!("{:x}", Sha256::digest(name.as_bytes()))})
        };
        value["read_commands"] =
            json!({"roots":["src"],"rg":executable("rg"),"cat":executable("cat")});
        value["command_templates"] = json!([]);
        value["allowed_operations"] = json!(["read", "command"]);
        value["write_scopes"] = json!([]);
        value["declared_targets"] = json!([]);
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value.clone())).unwrap();
        let rg = root.join("rg").display().to_string();
        let cat = root.join("cat").display().to_string();
        for command in [
            format!("{rg} --no-config --max-filesize=1M --files -- src"),
            format!("{rg} --no-config --max-filesize=1M -n -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -i -- different src"),
            format!("{rg} --no-config --max-filesize=1M -n -o -- 'ne+dle' src"),
            format!("{rg} --no-config --max-filesize=1M -c -w -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -v -x --count-matches -- needle src/new.txt"),
            format!("{rg} --no-config --max-filesize=1M -n -A 2 -B 1 -m 5 -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -C 3 --max-count=7 --context=2 -H --column -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -N -I -b --trim -q --files-without-match -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --files --max-depth 2 -g '*.txt' -- src"),
            format!("{rg} --no-config --max-filesize=1M -d 1 --max-depth=3 -n -- needle src"),
            format!("{cat} -- src/new.txt"),
        ] {
            admitted.check_command("bash", &command).unwrap();
        }
        fs::write(root.join("src/discovered-later.txt"), "new").unwrap();
        assert!(
            admitted
                .check_command("bash", &format!("{cat} -- src/discovered-later.txt"))
                .is_ok()
        );
        for command in [
            format!("{rg} --no-config --max-filesize=1M --pre evil -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --follow -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --hidden -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --no-ignore -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -z -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -f src/new.txt -- src"),
            format!("{rg} --no-config --max-filesize=1M -A x -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -A 10001 -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --context= -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -m -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --max-depth x -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --max-depth=10001 -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -- needle outside.txt"),
            format!("{rg} --max-filesize=1M -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -- needle src; touch bad"),
            format!("{cat} -- src/../outside.txt"),
            format!("{cat} -- src/.env"),
            "cat src/new.txt".to_string(),
        ] {
            assert!(
                admitted.check_command("bash", &command).is_err(),
                "{command}"
            );
        }
        assert!(
            admitted
                .check_path("modify", &root.join("src/new.txt"))
                .is_err()
        );
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(root.join("outside.txt"), root.join("src/link")).unwrap();
            assert!(
                admitted
                    .check_command("bash", &format!("{cat} -- src/link"))
                    .is_err()
            );
        }
        fs::write(root.join("rg"), "changed binary").unwrap();
        assert!(
            admitted
                .check_command(
                    "bash",
                    &format!("{rg} --no-config --max-filesize=1M --files -- src")
                )
                .is_err()
        );
        let sealed = reseal(value);
        let mut forged = sealed.clone();
        forged["read_commands"]["roots"] = json!(["outside"]);
        forged.as_object_mut().unwrap().remove("content_sha256");
        forged["content_sha256"] = json!(semantic_sha256(&forged));
        assert!(JSpaceMatcher::from_value(&root, &forged).is_err());
    }

    #[test]
    fn discovery_accepts_bounded_explicit_or_patterns() {
        let (_temp, root, value) = local_directory_source_read_fixture();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        let rg = root.join("rg").display().to_string();
        let prefix = format!("{rg} --no-config --max-filesize=1M");
        for command in [
            format!("{prefix} -n -F -e 'compile_local_context' -e 'verify_local_context' -e 'source_sections' -- src"),
            format!("{prefix} --regexp needle -- src/new.txt"),
            format!("{prefix} -e needle --regexp different -n -- src"),
            format!("{prefix} -e {} -- src", "x".repeat(2048)),
            format!("{prefix} -e {} -e {} -- src", "x".repeat(1024), "y".repeat(1024)),
            format!("{prefix} -e {} -- src", "\u{00e9}".repeat(1024)),
            format!("{prefix} {} -- src", vec!["-e needle"; 32].join(" ")),
            format!("{prefix} {} -- src", vec!["-e needle"; 33].join(" ")),
        ] {
            admitted.check_command("bash", &command).unwrap();
            admitted.check_read_search_command("bash", &command).unwrap();
        }
    }

    #[test]
    fn discovery_explicit_patterns_preserve_literal_values() {
        let (_temp, root, value) = local_directory_source_read_fixture();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        let command = format!(
            "{} --no-config --max-filesize=1M -F -e 'a|b' --regexp '--hidden' -e '--' -- src",
            root.join("rg").display()
        );
        let argv = super::parse_shell_argv(&command).unwrap();
        assert_eq!(&argv[4..], &["-e", "a|b", "--regexp", "--hidden", "-e", "--", "--", "src"]);
        admitted.check_command("bash", &command).unwrap();
        admitted.check_read_search_command("bash", &command).unwrap();
    }

    #[test]
    fn discovery_explicit_patterns_keep_bounds_and_scope_denials() {
        let (_temp, root, value) = local_directory_source_read_fixture();
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        let prefix = format!("{} --no-config --max-filesize=1M", root.join("rg").display());
        for command in [
            format!("{prefix} -e"),
            format!("{prefix} --regexp"),
            format!("{prefix} -e '' -- src"),
            format!("{prefix} -e -- src"),
            format!("{prefix} -e needle src"),
            format!("{prefix} -e needle --"),
            format!("{prefix} -e {} -- src", "x".repeat(2049)),
            format!("{prefix} -e {} -e {} -- src", "x".repeat(1024), "y".repeat(1025)),
            format!("{prefix} -e {} -- src", "\u{00e9}".repeat(1025)),
            format!("{prefix} --files -e needle -- src"),
            format!("{prefix} -e needle --files -- src"),
            format!("{prefix} -eneedle -- src"),
            format!("{prefix} --regexp=needle -- src"),
            format!("{prefix} -e needle -f src/new.txt -- src"),
            format!("{prefix} -e needle --file src/new.txt -- src"),
            format!("{prefix} -e needle --pre cat -- src"),
            format!("{prefix} -e needle --hidden -- src"),
            format!("{prefix} -e needle --follow -- src"),
            format!("{prefix} -e needle -- --hidden src"),
            format!("{prefix} -e needle -- -"),
            format!("{prefix} -e needle -- exact.txt"),
            format!("{prefix} -e needle -- exact.txt src"),
            format!("{prefix} -e needle -- src/../exact.txt"),
            format!("{prefix} -e needle -- src/.env"),
            format!("{prefix} -e needle -- ../outside"),
            format!("{prefix} -e needle -- src; touch bad"),
        ] {
            assert!(admitted.check_command("bash", &command).is_err(), "{command}");
            assert!(admitted.check_read_search_command("bash", &command).is_err(), "{command}");
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(root.join("exact.txt"), root.join("src/link")).unwrap();
            assert!(admitted.check_command("bash", &format!("{prefix} -e needle -- src/link")).is_err());
        }
    }

    #[test]
    fn discovery_refusals_name_the_argument_to_fix() {
        use sha2::{Digest, Sha256};
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        fs::create_dir(root.join("src")).unwrap();
        fs::write(root.join("src/new.txt"), "needle").unwrap();
        fs::write(root.join("outside.txt"), "outside").unwrap();
        let mut value = contract(&root);
        let executable = |name: &str| {
            let path = root.join(name);
            fs::write(&path, name).unwrap();
            json!({"path": path, "sha256": format!("{:x}", Sha256::digest(name.as_bytes()))})
        };
        value["read_commands"] =
            json!({"roots":["src"],"rg":executable("rg"),"cat":executable("cat")});
        value["command_templates"] = json!([]);
        value["allowed_operations"] = json!(["read", "command"]);
        value["write_scopes"] = json!([]);
        value["declared_targets"] = json!([]);
        let admitted = JSpaceMatcher::from_value(&root, &reseal(value)).unwrap();
        let rg = root.join("rg").display().to_string();
        let cat = root.join("cat").display().to_string();
        let refusal = |command: String| {
            admitted.check_command("bash", &command).unwrap_err().to_string()
        };
        let text = refusal(format!("{rg} --no-config --max-filesize=1M --hidden -- needle src"));
        assert!(text.contains("target=--hidden") && text.contains("unsupported rg option"), "{text}");
        let text = refusal(format!("{rg} --no-config --max-filesize=1M -n -- needle src/missing/deeper.txt"));
        assert!(
            text.contains("target=src/missing/deeper.txt") && text.contains("src/missing does not exist"),
            "{text}"
        );
        let text = refusal(format!("{rg} --no-config --max-filesize=1M -n -- needle outside.txt"));
        assert!(text.contains("target=outside.txt") && text.contains(r#"discovery roots ["src"]"#), "{text}");
        // A path outside the roots is refused as such before any filesystem lookup.
        let text = refusal(format!("{rg} --no-config --max-filesize=1M -n -- needle gone/x.txt"));
        assert!(text.contains("target=gone/x.txt") && text.contains("outside discovery roots"), "{text}");
        let text = refusal("cat src/new.txt".to_string());
        assert!(text.contains("target=cat") && text.contains(&format!("pinned executable {cat}")), "{text}");
        let text = refusal("node --check src/new.txt".to_string());
        assert!(text.contains("target=node") && text.contains("declared verifier commands"), "{text}");
        let text = refusal(format!("{cat} -- src"));
        assert!(text.contains("target=src") && text.contains("rg --files"), "{text}");
        let text = refusal(format!("{rg} --no-config --max-filesize=1M -A 99999 -- needle src"));
        assert!(text.contains("target=-A") && text.contains("0..10000"), "{text}");
        let long = "x".repeat(400);
        let text = refusal(format!("{rg} --no-config --max-filesize=1M --{long} -- needle src"));
        assert!(text.len() < 600 && text.contains("..."), "an echoed argument is bounded: {text}");
    }

    #[test]
    fn admission_rejects_changed_body_with_replayed_cached_digests() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let original = contract(root.path());
        let first = cache
            .admit("session", root.path(), Some(&original))
            .expect("initial admission")
            .expect("matcher");
        for field in ["read_scopes", "provenance"] {
            let mut tampered = original.clone();
            if field == "read_scopes" {
                tampered[field] = json!(["other/**"]);
            } else {
                tampered[field]["repo_head"] = json!("unverified-head");
            }
            // The untrusted request replays both already-admitted digest strings.
            // This is an integrity rejection, not a claim that the cached matcher
            // grants the forged scope.
            assert!(
                cache
                    .admit("session", root.path(), Some(&tampered))
                    .is_err(),
                "changed {field} was accepted solely from claimed digest strings"
            );
        }
        let reused = cache
            .admit("session", root.path(), Some(&original))
            .expect("original remains reusable")
            .expect("matcher");
        assert!(std::sync::Arc::ptr_eq(&first, &reused));
        assert_eq!(cache.admissions(), 1);
    }

    #[test]
    fn admission_checks_exact_cached_numeric_representation() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let mut original = contract(root.path());
        original["provenance"]["metric"] = json!(-0.0);
        let original = reseal(original);
        cache
            .admit("session", root.path(), Some(&original))
            .expect("initial admission");
        let mut changed = original.clone();
        changed["provenance"]["metric"] = json!(0.0);
        let error = cache
            .admit("session", root.path(), Some(&changed))
            .expect_err("a byte-distinct body cannot replay cached digests");
        assert_eq!(error.code(), "JSPACE_CONTENT_DIGEST_MISMATCH");
    }

    #[test]
    fn concurrent_same_session_admissions_publish_one_matcher() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let value = contract(root.path());
        let barrier = std::sync::Barrier::new(8);
        std::thread::scope(|scope| {
            let workers = (0..8)
                .map(|_| {
                    let (cache, value, barrier, root) = (&cache, &value, &barrier, root.path());
                    scope.spawn(move || {
                        barrier.wait();
                        cache
                            .admit("session", root, Some(value))
                            .expect("admit")
                            .expect("matcher")
                    })
                })
                .collect::<Vec<_>>();
            let matchers = workers
                .into_iter()
                .map(|worker| worker.join().expect("worker"))
                .collect::<Vec<_>>();
            assert!(
                matchers
                    .iter()
                    .all(|matcher| std::sync::Arc::ptr_eq(&matchers[0], matcher))
            );
        });
        assert_eq!(cache.admissions(), 1);
    }

    #[test]
    fn admission_reuses_same_digest_and_rejects_changed_digest() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let value = contract(root.path());
        let first = cache
            .admit("session", root.path(), Some(&value))
            .expect("first admission");
        let second = cache
            .admit("session", root.path(), Some(&value))
            .expect("reuse");
        assert!(std::sync::Arc::ptr_eq(
            &first.expect("matcher"),
            &second.expect("matcher")
        ));
        assert_eq!(cache.admissions(), 1);

        let mut changed = value.clone();
        changed["read_scopes"] = json!(["other/**"]);
        let changed = reseal(changed);
        let error = cache
            .admit("session", root.path(), Some(&changed))
            .expect_err("digest change");
        assert_eq!(error.code(), "JSPACE_CONTRACT_CHANGED");
    }

    #[test]
    fn bound_session_cannot_omit_contract_on_retry() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let value = contract(root.path());
        cache
            .admit("session", root.path(), Some(&value))
            .expect("first admission");

        let error = cache
            .admit("session", root.path(), None)
            .expect_err("bound retry must not omit contract");
        assert_eq!(error.code(), "JSPACE_CONTRACT_MISSING_ON_RETRY");
        assert!(
            cache
                .admit("unbound-session", root.path(), None)
                .expect("unbound legacy session")
                .is_none()
        );
    }

    #[test]
    fn exact_two_domain_git_worktree_effects_are_admitted_and_projected() {
        let (root, worktree, metadata) = effect_topology();
        let value = effect_contract(root.path(), &worktree, &metadata);
        let matcher = JSpaceMatcher::from_value(root.path(), &value).expect("effect matcher");
        let command = format!(
            "git --git-dir {} worktree remove {}",
            fs::canonicalize(metadata.join("worktrees/task"))
                .expect("metadata target")
                .display(),
            fs::canonicalize(&worktree).expect("worktree").display()
        );
        matcher
            .check_command("shell_command", &command)
            .expect("two-domain command");

        let mut expected_writes = vec![
            fs::canonicalize(metadata.join("worktrees/task"))
                .expect("metadata target")
                .display()
                .to_string(),
            fs::canonicalize(&worktree)
                .expect("worktree")
                .display()
                .to_string(),
        ];
        expected_writes.sort();
        assert_eq!(matcher.scope_projection().write_scopes, expected_writes);
        assert_eq!(matcher.declared_targets(), expected_writes);
        assert_eq!(
            matcher.scope_projection().read_scopes,
            vec![
                fs::canonicalize(metadata.join("worktrees/task"))
                    .expect("metadata target")
                    .display()
                    .to_string()
            ]
        );
    }

    #[test]
    fn effect_capability_normalization_is_lossless_and_permission_sensitive() {
        let (root, worktree, metadata) = effect_topology();
        let original = effect_contract(root.path(), &worktree, &metadata);
        let original_authorization = original["authorization_semantic_sha256"].clone();
        let mut equivalent = original.clone();
        equivalent
            .as_object_mut()
            .expect("object")
            .remove("content_sha256");
        equivalent["matched_surface_ids"] = json!(["surface", "surface"]);
        equivalent["effect_capabilities"]
            .as_array_mut()
            .expect("capabilities")
            .reverse();
        equivalent["command_templates"][0]["effects"] = json!(["delete", "read"]);
        equivalent["command_templates"][0]["targets"]
            .as_array_mut()
            .expect("targets")
            .reverse();
        let equivalent = reseal(equivalent);
        assert_eq!(
            equivalent["authorization_semantic_sha256"],
            original_authorization
        );
        assert_ne!(equivalent["content_sha256"], original["content_sha256"]);

        let cache = JSpaceAdmissionCache::default();
        cache
            .admit("effect-session", root.path(), Some(&original))
            .expect("original admission");
        cache
            .admit("effect-session", root.path(), Some(&equivalent))
            .expect("equivalent normalization refresh");

        let mut expanded = original;
        expanded["effect_capabilities"][0]["allowed_operations"] =
            json!(["read", "modify", "delete"]);
        let expanded = reseal(expanded);
        assert_ne!(
            expanded["authorization_semantic_sha256"],
            original_authorization
        );
        assert_eq!(
            cache
                .admit("effect-session", root.path(), Some(&expanded))
                .expect_err("permission expansion")
                .code(),
            "JSPACE_CONTRACT_CHANGED"
        );
    }

    #[test]
    fn multi_domain_admission_rejects_missing_outside_broad_and_overlapping_domains() {
        let (root, worktree, metadata) = effect_topology();
        let value = effect_contract(root.path(), &worktree, &metadata);

        let mut missing = value.clone();
        missing["command_templates"][0]["targets"][0]
            .as_object_mut()
            .expect("target")
            .remove("domain_id");
        assert_eq!(
            authorization_semantic_sha256(&missing)
                .expect_err("missing domain")
                .code(),
            "JSPACE_CONTRACT_MALFORMED"
        );

        let outside = root.path().join("outside");
        fs::create_dir(&outside).expect("outside");
        let outside = fs::canonicalize(outside).expect("canonical outside");
        let mut outside_target = value.clone();
        outside_target["command_templates"][0]["argv"][5] = json!(outside);
        outside_target["command_templates"][0]["targets"][2]["path"] = json!(outside);
        assert_eq!(
            authorization_semantic_sha256(&outside_target)
                .expect_err("outside target")
                .code(),
            "JSPACE_PATH_OUTSIDE_DOMAIN"
        );

        let mut faked_target = value.clone();
        faked_target["command_templates"][0]["targets"][2]["path"] = json!(metadata);
        assert_eq!(
            authorization_semantic_sha256(&faked_target)
                .expect_err("target must match its argv identity")
                .code(),
            "JSPACE_COMMAND_TARGET_ARGV_MISMATCH"
        );

        let mut broad = value.clone();
        broad["effect_capabilities"][0]["root"] = json!("/");
        assert_eq!(
            authorization_semantic_sha256(&broad)
                .expect_err("broad root")
                .code(),
            "JSPACE_DOMAIN_ROOT_TOO_BROAD"
        );

        let nested = fs::canonicalize(metadata.join("worktrees/task"))
            .expect("metadata target")
            .join("nested");
        fs::create_dir(&nested).expect("nested");
        let mut overlapping = value;
        overlapping["effect_capabilities"][1]["root"] = json!(nested);
        assert_eq!(
            authorization_semantic_sha256(&overlapping)
                .expect_err("overlapping roots")
                .code(),
            "JSPACE_DOMAIN_ROOT_OVERLAP"
        );
    }

    #[test]
    fn every_effect_domain_argv_path_requires_an_exact_target_identity() {
        let (root, worktree, metadata) = effect_topology();
        let mut value = effect_contract(root.path(), &worktree, &metadata);
        value["effect_capabilities"][0]["allowed_operations"] = json!(["delete"]);
        value["command_templates"][0]["effects"] = json!(["delete"]);
        value["command_templates"][0]["targets"] = json!([{
            "domain_id": "worktree",
            "operation": "delete",
            "path": fs::canonicalize(&worktree).expect("worktree"),
            "binding": "argv",
            "argv_index": 5
        }]);
        let error = authorization_semantic_sha256(&value)
            .expect_err("metadata argv target cannot be omitted even for same operation kind");
        assert_eq!(error.code(), "JSPACE_COMMAND_MUTATION_TARGET_MISSING");
    }

    #[test]
    fn multi_domain_mutation_rejects_untyped_or_ambiguous_command_shapes() {
        let (root, worktree, metadata) = effect_topology();
        let value = effect_contract(root.path(), &worktree, &metadata);

        let mut hidden = value.clone();
        hidden["command_templates"][0]["targets"][1]["binding"] = json!("implicit_exact");
        hidden["command_templates"][0]["targets"][1]
            .as_object_mut()
            .expect("target")
            .remove("argv_index");
        assert_eq!(
            authorization_semantic_sha256(&hidden)
                .expect_err("hidden effect target")
                .code(),
            "JSPACE_COMMAND_TARGET_BINDING_INVALID"
        );

        let outside = root.path().join("outside-command-target");
        fs::create_dir(&outside).expect("outside command target");
        let mut absolute = value.clone();
        absolute["command_templates"][0]["argv"] =
            json!(["rm", fs::canonicalize(outside).expect("outside")]);
        absolute["command_templates"][0]["effects"] = json!(["delete"]);
        absolute["command_templates"][0]["targets"] = json!([{
            "domain_id": "worktree",
            "operation": "delete",
            "path": fs::canonicalize(&worktree).expect("worktree"),
            "binding": "argv",
            "argv_index": 1
        }]);
        assert_eq!(
            authorization_semantic_sha256(&absolute)
                .expect_err("outside absolute mutation path")
                .code(),
            "JSPACE_COMMAND_TARGET_ARGV_MISMATCH"
        );

        let mut relative = value.clone();
        relative["command_templates"][0]["argv"] = json!(["rm", "victim"]);
        relative["command_templates"][0]["effects"] = json!(["delete"]);
        relative["command_templates"][0]["targets"] = json!([]);
        assert_eq!(
            authorization_semantic_sha256(&relative)
                .expect_err("relative mutation path")
                .code(),
            "JSPACE_COMMAND_MUTATION_TARGET_MISSING"
        );

        let mut disguised = value.clone();
        disguised["effect_capabilities"][1]["read_scopes"] = json!(["."]);
        disguised["effect_capabilities"][1]["allowed_operations"] = json!(["read", "delete"]);
        disguised["command_templates"][0]["argv"] =
            json!(["rm", fs::canonicalize(&worktree).expect("worktree")]);
        disguised["command_templates"][0]["effects"] = json!(["read"]);
        disguised["command_templates"][0]["targets"] = json!([{
            "domain_id": "worktree",
            "operation": "read",
            "path": fs::canonicalize(&worktree).expect("worktree"),
            "binding": "argv",
            "argv_index": 1
        }]);
        assert_eq!(
            authorization_semantic_sha256(&disguised)
                .expect_err("mutation command disguised as read")
                .code(),
            "JSPACE_MULTI_DOMAIN_COMMAND_UNSUPPORTED"
        );

        let mut incomplete_semantics = value.clone();
        incomplete_semantics["effect_capabilities"][1]["read_scopes"] = json!(["."]);
        incomplete_semantics["effect_capabilities"][1]["allowed_operations"] =
            json!(["read", "delete"]);
        incomplete_semantics["command_templates"][0]["effects"] = json!(["read"]);
        incomplete_semantics["command_templates"][0]["targets"] = json!([{
            "domain_id": "git_metadata",
            "operation": "read",
            "path": fs::canonicalize(metadata.join("worktrees/task"))
                .expect("metadata target"),
            "binding": "argv",
            "argv_index": 2
        }, {
            "domain_id": "worktree",
            "operation": "read",
            "path": fs::canonicalize(&worktree).expect("worktree"),
            "binding": "argv",
            "argv_index": 5
        }]);
        assert_eq!(
            authorization_semantic_sha256(&incomplete_semantics)
                .expect_err("git mutation semantics cannot be under-declared")
                .code(),
            "JSPACE_COMMAND_SEMANTICS_MISMATCH"
        );

        let mut embedded = value;
        embedded["command_templates"][0]["argv"] = json!([
            "git",
            "--git-dir=/outside",
            "worktree",
            "remove",
            fs::canonicalize(&worktree).expect("worktree")
        ]);
        embedded["command_templates"][0]["effects"] = json!(["delete"]);
        embedded["command_templates"][0]["targets"] = json!([{
            "domain_id": "worktree",
            "operation": "delete",
            "path": fs::canonicalize(&worktree).expect("worktree"),
            "binding": "argv",
            "argv_index": 4
        }]);
        assert_eq!(
            authorization_semantic_sha256(&embedded)
                .expect_err("embedded metadata path")
                .code(),
            "JSPACE_MULTI_DOMAIN_COMMAND_UNSUPPORTED"
        );
    }

    #[test]
    fn domain_symlinks_and_post_admission_symlink_escapes_fail_closed() {
        let (root, worktree, metadata) = effect_topology();
        let domain_alias = root.path().join("metadata-alias");
        std::os::unix::fs::symlink(metadata.join("worktrees/task"), &domain_alias)
            .expect("domain alias");
        let mut aliased = effect_contract(root.path(), &worktree, &metadata);
        aliased["effect_capabilities"][0]["root"] = json!(domain_alias);
        assert_eq!(
            authorization_semantic_sha256(&aliased)
                .expect_err("domain alias")
                .code(),
            "JSPACE_DOMAIN_ROOT_SYMLINK"
        );

        let mut value = effect_contract(root.path(), &worktree, &metadata);
        value["effect_capabilities"][1]["write_scopes"] = json!(["future/victim"]);
        value["effect_capabilities"][1]["declared_targets"] = json!(["future/victim"]);
        value["command_templates"] = json!([]);
        value["allowed_operations"] = json!([]);
        let value = reseal(value);
        let matcher = JSpaceMatcher::from_value(root.path(), &value).expect("matcher");
        let outside = root.path().join("outside-post-admission");
        fs::create_dir(&outside).expect("outside");
        let canonical_worktree = fs::canonicalize(&worktree).expect("canonical worktree");
        std::os::unix::fs::symlink(&outside, canonical_worktree.join("future"))
            .expect("future escape");
        assert_eq!(
            matcher
                .check_path("delete", &canonical_worktree.join("future/victim"))
                .expect_err("post-admission symlink escape")
                .code(),
            "JSPACE_SYMLINK_ESCAPE"
        );
    }

    #[test]
    fn admission_rejects_root_change_for_an_existing_session() {
        let root = tempfile::tempdir().expect("root");
        let other = tempfile::tempdir().expect("other");
        let cache = JSpaceAdmissionCache::default();
        let value = contract(root.path());
        cache
            .admit("session", root.path(), Some(&value))
            .expect("first admission");

        let error = cache
            .admit("session", other.path(), Some(&value))
            .expect_err("root change");
        assert_eq!(error.code(), "JSPACE_ROOT_MISMATCH");
    }

    #[test]
    fn admission_rejects_dcf_evidence_root_that_differs_from_contract_root() {
        let root = tempfile::tempdir().expect("root");
        let other = tempfile::tempdir().expect("other");
        let mut value = contract(root.path());
        value["dcf_generation"]["repo_root"] = json!(other.path());
        let value = reseal(value);

        let error = JSpaceMatcher::from_value(root.path(), &value)
            .expect_err("foreign DCF evidence root must fail");
        assert_eq!(error.code(), "JSPACE_DCF_ROOT_MISMATCH");
    }

    #[test]
    fn scope_projection_is_canonical_sorted_unique_and_order_invariant() {
        let root = tempfile::tempdir().expect("root");
        let mut first = contract(root.path());
        first["read_scopes"] = json!(["zeta/**", "./src/**", "docs/./guide.md", "src/**"]);
        first["write_scopes"] = json!(["src/main.rs", "./src/**", "src/**"]);
        let first = JSpaceMatcher::from_value(root.path(), &reseal(first)).expect("first matcher");

        let mut second = contract(root.path());
        second["read_scopes"] = json!(["src/**", "docs/guide.md", "zeta/**", "./src/**"]);
        second["write_scopes"] = json!(["src/**", "src/main.rs", "./src/**"]);
        let second =
            JSpaceMatcher::from_value(root.path(), &reseal(second)).expect("second matcher");

        let expected = JSpaceScopeProjection {
            read_scopes: vec![
                "docs/guide.md".to_string(),
                "src/**".to_string(),
                "zeta/**".to_string(),
            ],
            write_scopes: vec!["src/**".to_string(), "src/main.rs".to_string()],
        };
        assert_eq!(first.scope_projection(), &expected);
        assert_eq!(second.scope_projection(), &expected);
    }

    #[test]
    fn scope_overlap_covers_exact_recursive_prefix_and_disjoint_claims() {
        assert!(scope_claims_overlap("src/main.rs", "./src/main.rs").expect("exact"));
        assert!(scope_claims_overlap("src/**", "src/nested/file.rs").expect("prefix"));
        assert!(scope_claims_overlap("src/nested/**", "src/**").expect("reverse prefix"));
        assert!(!scope_claims_overlap("src/main.rs", "src/lib.rs").expect("exact disjoint"));
        assert!(!scope_claims_overlap("src/**", "tests/**").expect("recursive disjoint"));
    }

    #[test]
    fn scope_projection_conflict_includes_write_read_but_not_read_read() {
        let writer = JSpaceScopeProjection {
            read_scopes: vec![],
            write_scopes: vec!["src/**".to_string()],
        };
        let reader = JSpaceScopeProjection {
            read_scopes: vec!["src/main.rs".to_string()],
            write_scopes: vec![],
        };
        assert!(scope_projections_conflict(&writer, &reader).expect("write-read"));
        assert!(scope_projections_conflict(&reader, &writer).expect("read-write"));

        let other_reader = JSpaceScopeProjection {
            read_scopes: vec!["src/**".to_string()],
            write_scopes: vec![],
        };
        assert!(!scope_projections_conflict(&reader, &other_reader).expect("read-read"));
    }

    #[test]
    fn scope_overlap_rejects_traversal_and_absolute_claims() {
        let traversal =
            scope_claims_overlap("../secrets", "src/**").expect_err("traversal must fail closed");
        assert_eq!(traversal.code(), "JSPACE_PATH_TRAVERSAL");

        let absolute =
            scope_claims_overlap("/workspace/src/**", "src/**").expect_err("absolute scope");
        assert_eq!(absolute.code(), "JSPACE_SCOPE_REPRESENTATION_MISMATCH");
    }

    #[test]
    fn admission_refreshes_provenance_without_changing_authorization() {
        let root = tempfile::tempdir().expect("root");
        let cache = JSpaceAdmissionCache::default();
        let value = contract(root.path());
        let first_authorization = value["authorization_semantic_sha256"].clone();
        cache
            .admit("session", root.path(), Some(&value))
            .expect("first admission");

        let mut refreshed = value.clone();
        refreshed["dcf_generation"]["generation_id"] = json!("g-next");
        refreshed["provenance"]["repo_head"] = json!("head-next");
        let refreshed = reseal(refreshed);
        assert_eq!(
            refreshed["authorization_semantic_sha256"],
            first_authorization
        );
        assert_ne!(refreshed["content_sha256"], value["content_sha256"]);
        cache
            .admit("session", root.path(), Some(&refreshed))
            .expect("provenance-only refresh");
        assert_eq!(cache.admissions(), 1);
    }

    #[test]
    fn path_operation_and_command_checks_are_local_and_fail_closed() {
        let root = tempfile::tempdir().expect("root");
        fs::create_dir(root.path().join("src")).expect("src");
        let matcher =
            JSpaceMatcher::from_value(root.path(), &contract(root.path())).expect("matcher");
        matcher
            .check_path("read", &root.path().join("src").join("main.rs"))
            .expect("read");
        matcher
            .check_path("modify", &root.path().join("src").join("main.rs"))
            .expect("modify");
        assert_eq!(
            matcher
                .check_path("delete", &root.path().join("src").join("main.rs"))
                .expect_err("delete")
                .code(),
            "JSPACE_OPERATION_DENIED"
        );
        assert_eq!(
            matcher
                .check_path("modify", &root.path().join("src").join("other.rs"))
                .expect_err("undeclared target")
                .code(),
            JSPACE_EXPANSION_REQUIRED
        );
        assert!(
            matcher
                .check_command("shell_command", "git status --short")
                .is_ok()
        );
        assert_eq!(
            matcher
                .check_command(
                    "shell_command",
                    "git status --short; curl https://example.test"
                )
                .expect_err("shell chaining")
                .code(),
            "JSPACE_COMMAND_SYNTAX_UNSAFE"
        );
        assert_eq!(
            matcher
                .check_command("shell_command", "git status")
                .expect_err("command")
                .code(),
            "JSPACE_COMMAND_DENIED"
        );
    }

    #[test]
    fn metadata_and_patch_entry_do_not_require_a_shell_grant() {
        let root = tempfile::tempdir().expect("root");
        fs::create_dir(root.path().join("src")).expect("src");
        let mut value = contract(root.path());
        value["allowed_operations"] = json!(["read", "modify"]);
        value["denied_operations"] = json!(["command", "create", "delete", "network"]);
        value["command_templates"] = json!([]);
        let matcher = JSpaceMatcher::from_value(root.path(), &reseal(value)).expect("matcher");

        matcher
            .check_command(
                "task_status",
                r#"{"task_group":"single file","task_type":["new_build"]}"#,
            )
            .expect("internal task status");
        matcher
            .check_command("apply_patch", "*** Begin Patch\n*** End Patch")
            .expect("patch entry; effect paths are checked separately");
        matcher
            .check_path("modify", &root.path().join("src/main.rs"))
            .expect("declared patch target");
        assert_eq!(
            matcher
                .check_path("modify", &root.path().join("src/other.rs"))
                .expect_err("undeclared patch target")
                .code(),
            JSPACE_EXPANSION_REQUIRED
        );
        assert_eq!(
            matcher
                .check_command("shell_command", "git status --short")
                .expect_err("shell command remains denied")
                .code(),
            "JSPACE_OPERATION_DENIED"
        );
    }

    #[test]
    fn traversal_and_symlink_escape_are_rejected_before_scope_lookup() {
        let root = tempfile::tempdir().expect("root");
        let outside = tempfile::tempdir().expect("outside");
        fs::create_dir(root.path().join("src")).expect("src");
        std::os::unix::fs::symlink(outside.path(), root.path().join("src").join("link"))
            .expect("link");
        let matcher =
            JSpaceMatcher::from_value(root.path(), &contract(root.path())).expect("matcher");
        assert_eq!(
            matcher
                .check_path("read", std::path::Path::new("../outside"))
                .expect_err("traversal")
                .code(),
            "JSPACE_PATH_TRAVERSAL"
        );
        assert_eq!(
            matcher
                .check_path("read", &root.path().join("src/link/file"))
                .expect_err("symlink")
                .code(),
            "JSPACE_SYMLINK_ESCAPE"
        );
    }
}
