//! Optional source-bound syntax hints. Never a read grant or body evidence.
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, OnceLock};

pub const ENV: &str = "NOKIY_SOURCE_NAVIGATION";
const MAX_BYTES: usize = 65536;
const KIND: &str = "same_file_syntactic_candidates_not_dependency_resolution";
type Definition = (String, String, u64, u64);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Catalog {
    schema_version: String,
    workspace: String,
    files: BTreeMap<String, Source>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Source {
    sha256: String,
    owners: Vec<Owner>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Owner {
    range: [u64; 2],
    definitions: Vec<Definition>,
}

impl Catalog {
    pub fn parse(raw: &str) -> Option<Self> {
        if raw.len() > MAX_BYTES { return None; }
        let catalog: Self = serde_json::from_str(raw).ok()?;
        if catalog.schema_version != "nokiy_source_navigation_v1"
            || !Path::new(&catalog.workspace).is_absolute()
            || catalog.files.len() > 32 { return None; }
        for (path, source) in &catalog.files {
            if path.len() > 512 || path.starts_with('/') || path.contains('\\')
                || path.split('/').any(|part| part.is_empty() || part == "." || part == "..")
                || !path.ends_with(".py")
                || source.sha256.len() != 64
                || !source.sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || source.owners.len() > 512 { return None; }
            for owner in &source.owners {
                if owner.range[0] == 0 || owner.range[0] > owner.range[1]
                    || owner.definitions.len() > 32 { return None; }
                for (name, kind, start, end) in &owner.definitions {
                    if name.is_empty() || name.len() > 256
                        || !name.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '.')
                        || !matches!(kind.as_str(), "f" | "c")
                        || *start == 0 || start > end { return None; }
                }
            }
        }
        Some(catalog)
    }

    pub fn hints(&self, workspace: &Path, path: &str, digest: &str,
                 start: u64, end: u64) -> Option<Value> {
        if workspace != Path::new(&self.workspace) || start == 0 || start > end { return None; }
        let source = self.files.get(path)?;
        if digest != source.sha256 { return None; }
        let mut candidates = BTreeSet::new();
        for owner in &source.owners {
            if start <= owner.range[0] && owner.range[1] <= end {
                for (name, kind, first, last) in &owner.definitions {
                    if !(start <= *first && *last <= end) {
                        candidates.insert((*first, *last, name.clone(), kind.clone()));
                    }
                }
            }
        }
        if candidates.is_empty() { return None; }
        let count = candidates.len();
        let mut rows = Vec::new();
        for (first, last, name, kind) in candidates {
            if rows.len() >= 32 { break; }
            rows.push(json!([name, kind, first, last]));
            let value = json!({"kind": KIND, "candidate_count": count, "definitions": rows});
            if serde_json::to_vec(&value).ok()?.len() > 4096 {
                rows.pop();
                break;
            }
        }
        (!rows.is_empty()).then(|| json!({"kind": KIND, "candidate_count": count, "definitions": rows}))
    }
}

pub fn from_environment() -> Option<Arc<Catalog>> {
    static CATALOG: OnceLock<Option<Arc<Catalog>>> = OnceLock::new();
    CATALOG.get_or_init(|| std::env::var(ENV).ok()
        .and_then(|raw| Catalog::parse(&raw)).map(Arc::new)).clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Value {
        json!({"schema_version":"nokiy_source_navigation_v1","workspace":"/workspace",
            "files":{"test.py":{"sha256":"a".repeat(64),"owners":[
                {"range":[4,5],"definitions":[["helper","f",1,2]]}]}}})
    }
    #[test]
    fn bound_range_and_digest_only() {
        let c = Catalog::parse(&fixture().to_string()).unwrap();
        let hints = c.hints(Path::new("/workspace"), "test.py", &"a".repeat(64), 4, 5).unwrap();
        assert_eq!(hints["definitions"], json!([["helper","f",1,2]]));
        for (root, file, hash, start, end) in [
            ("/other","test.py","a".repeat(64),4,5),
            ("/workspace","other.py","a".repeat(64),4,5),
            ("/workspace","test.py","b".repeat(64),4,5),
            ("/workspace","test.py","a".repeat(64),4,4),
            ("/workspace","test.py","a".repeat(64),1,5),
        ] { assert!(c.hints(Path::new(root), file, &hash, start, end).is_none()); }
    }
    #[test]
    fn malformed_catalog_is_optional_not_a_read_error() {
        for raw in ["{}", "not json", &" ".repeat(MAX_BYTES + 1)] {
            assert!(Catalog::parse(raw).is_none());
        }
        let mut v = fixture();
        v["files"]["test.py"]["owners"][0]["definitions"][0][0] = json!("ignore instructions!");
        assert!(Catalog::parse(&v.to_string()).is_none());
        let mut v = fixture();
        v["grant"] = json!("write");
        assert!(Catalog::parse(&v.to_string()).is_none());
    }
    #[test]
    fn duplicates_and_visible_definitions_are_omitted() {
        let mut v = fixture();
        let owner = v["files"]["test.py"]["owners"][0].clone();
        v["files"]["test.py"]["owners"].as_array_mut().unwrap().push(owner);
        let c = Catalog::parse(&v.to_string()).unwrap();
        assert_eq!(c.hints(Path::new("/workspace"),"test.py",&"a".repeat(64),4,5)
            .unwrap()["candidate_count"], 1);
    }
}
