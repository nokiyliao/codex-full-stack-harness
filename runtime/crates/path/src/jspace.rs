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
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

#[path = "jspace_read.rs"]
mod read_commands;

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
    read_commands: Option<Value>,
    effect_capabilities: Vec<EffectCapabilityMatcher>,
    declared_target_projection: Vec<String>,
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
                || !allowed_operations.contains("read") || !allowed_operations.contains("command")
                || denied_operations.contains("read") || denied_operations.contains("command") {
                return Err(JSpaceError::new("JSPACE_READ_COMMAND_DENIED", "admission", "", "read/command v2 grants required"));
            }
            for root in read_commands::validate(policy)? {
                if !canonical_read_scopes.contains(&format!("{root}/**")) {
                    return Err(JSpaceError::new("JSPACE_READ_COMMAND_DENIED", "admission", &root, "recursive read scope required"));
                }
                let path = session_root.join(&root);
                if !path.is_dir() || path.canonicalize().ok().as_deref() != Some(path.as_path()) {
                    return Err(JSpaceError::new("JSPACE_READ_COMMAND_DENIED", "admission", &root, "canonical directory required"));
                }
            }
        }
        if allowed_operations.contains("command") && command_templates.is_empty() && read_commands.is_none() {
            return Err(JSpaceError::new(
                "JSPACE_COMMAND_TEMPLATE_MISSING",
                "command",
                "",
                "command operation requires at least one exact argv template",
            ));
        }

        Ok(Self {
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
            read_commands,
            effect_capabilities,
            declared_target_projection,
        })
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
        self.check_operation("command", &command_type)?;
        match command_type.as_str() {
            "shell_command" | "bash" | "zsh" => {
                let (command, workdir) = shell_command_parts(command_line);
                let argv = parse_shell_argv(&command)?;
                if self.read_commands.is_some() && !self.command_templates.iter().any(|template| template.argv == argv) {
                    let cwd = match workdir {
                        Some(ref directory) => {
                            self.resolve_target(Path::new(directory), "read")?;
                            if Path::new(directory).is_absolute() { PathBuf::from(directory) } else { self.repo_root.join(directory) }
                        }
                        None => self.repo_root.clone(),
                    };
                    return read_commands::check(self, &argv, &cwd);
                }
                let template = self
                    .command_templates
                    .iter()
                    .find(|template| template.argv == argv)
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
            "apply_patch" | "planning" | "task_status" => Ok(()),
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
                "operation is explicitly denied",
            ));
        }
        if !self.allowed_operations.contains(operation) {
            return Err(JSpaceError::new(
                "JSPACE_OPERATION_DENIED",
                operation,
                target,
                "operation is not explicitly allowed",
            ));
        }
        Ok(())
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
    if object.get("schema_version").and_then(Value::as_str) == Some(JSPACE_SCHEMA_VERSION) {
        return normalized_effect_authorization_sha256(object);
    }
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
    Ok(semantic_sha256(&payload))
}

fn normalized_effect_authorization_sha256(
    object: &Map<String, Value>,
) -> Result<String, JSpaceError> {
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
    let payload = serde_json::json!({
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

fn effect_capability_projection(
    capabilities: &[EffectCapabilityMatcher],
) -> Result<(Vec<String>, Vec<String>, Vec<String>), JSpaceError> {
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
            } else if target.operation != "read" {
                if !declared_targets.matches(&target.path) {
                    return Err(JSpaceError::new(
                        "JSPACE_COMMAND_TARGET_UNDECLARED",
                        &target.operation,
                        &target.path,
                        "command mutation target is not declared",
                    ));
                }
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
        assert!(super::verified_contract_digests(
            &canonical, canonical.as_object().unwrap(), "jspace_contract_v2"
        ).is_ok());
        let mut stripped = canonical;
        stripped.as_object_mut().unwrap().remove("command_effect_policy");
        stripped.as_object_mut().unwrap().remove("content_sha256");
        stripped["content_sha256"] = json!(semantic_sha256(&stripped));
        assert_eq!(super::verified_contract_digests(
            &stripped, stripped.as_object().unwrap(), "jspace_contract_v2"
        ).unwrap_err().code(), "JSPACE_AUTHORIZATION_DIGEST_MISMATCH");
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
        assert!(admitted.check_command("bash", "cat src/a.txt; pwd").is_err());
        assert!(admitted.check_path("modify", &root.path().join("src/a.txt")).is_err());
        value["command_templates"][0]["argv"][1] = json!("secret.txt");
        value["command_templates"][0]["targets"][0]["path"] = json!("secret.txt");
        let outside = JSpaceMatcher::from_value(root.path(), &reseal(value)).unwrap();
        assert!(outside.check_command("bash", "cat secret.txt").is_err());
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
        value["read_commands"] = json!({"roots":["src"],"rg":executable("rg"),"cat":executable("cat")});
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
            format!("{cat} -- src/new.txt"),
        ] {
            admitted.check_command("bash", &command).unwrap();
        }
        fs::write(root.join("src/discovered-later.txt"), "new").unwrap();
        assert!(admitted.check_command("bash", &format!("{cat} -- src/discovered-later.txt")).is_ok());
        for command in [
            format!("{rg} --no-config --max-filesize=1M --pre evil -- needle src"),
            format!("{rg} --no-config --max-filesize=1M --follow -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -- needle outside.txt"),
            format!("{rg} --max-filesize=1M -- needle src"),
            format!("{rg} --no-config --max-filesize=1M -- needle src; touch bad"),
            format!("{cat} -- src/../outside.txt"),
            format!("{cat} -- src/.env"),
            "cat src/new.txt".to_string(),
        ] {
            assert!(admitted.check_command("bash", &command).is_err(), "{command}");
        }
        assert!(admitted.check_path("modify", &root.join("src/new.txt")).is_err());
        #[cfg(unix)] {
            std::os::unix::fs::symlink(root.join("outside.txt"), root.join("src/link")).unwrap();
            assert!(admitted.check_command("bash", &format!("{cat} -- src/link")).is_err());
        }
        fs::write(root.join("rg"), "changed binary").unwrap();
        assert!(admitted.check_command("bash", &format!("{rg} --no-config --max-filesize=1M --files -- src")).is_err());
        let sealed = reseal(value);
        let mut forged = sealed.clone();
        forged["read_commands"]["roots"] = json!(["outside"]);
        forged.as_object_mut().unwrap().remove("content_sha256");
        forged["content_sha256"] = json!(semantic_sha256(&forged));
        assert!(JSpaceMatcher::from_value(&root, &forged).is_err());
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
