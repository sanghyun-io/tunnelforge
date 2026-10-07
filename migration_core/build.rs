// Embeds Windows VERSIONINFO into tunnelforge-core.exe (SignPath requires
// ProductName/ProductVersion). The version comes from ../src/version.py, the
// single source of truth; Cargo.toml's package version is not kept in sync.
use std::{env, fs, path::Path};

fn app_version() -> String {
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let path = Path::new(&manifest_dir).join("../src/version.py");
    println!("cargo:rerun-if-changed={}", path.display());
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    text.lines()
        .find_map(|line| {
            let value = line.trim().strip_prefix("__version__")?.trim().strip_prefix('=')?;
            Some(value.trim().trim_matches(|c| c == '"' || c == '\'').to_string())
        })
        .unwrap_or_else(|| panic!("__version__ not found in {}", path.display()))
}

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let version = app_version();
    let mut packed = 0u64;
    for (i, part) in version.split('-').next().unwrap().split('.').take(4).enumerate() {
        let n: u64 = part.parse().unwrap_or_else(|_| panic!("bad version {version}"));
        packed |= n << (48 - 16 * i);
    }

    let mut res = winresource::WindowsResource::new();
    res.set("ProductName", "TunnelForge")
        .set("ProductVersion", &version)
        .set("FileVersion", &version)
        .set("FileDescription", "TunnelForge DB core service")
        .set("CompanyName", "sanghyun-io")
        .set("LegalCopyright", "MIT License")
        .set("OriginalFilename", "tunnelforge-core.exe")
        .set("InternalName", "tunnelforge-core")
        .set_version_info(winresource::VersionInfo::FILEVERSION, packed)
        .set_version_info(winresource::VersionInfo::PRODUCTVERSION, packed);
    res.compile().expect("failed to embed Windows VERSIONINFO");
}
