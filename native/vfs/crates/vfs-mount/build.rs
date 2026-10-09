fn main() {
    println!("cargo:rerun-if-changed=src/windows/bridge.c");
    println!("cargo:rerun-if-env-changed=WINFSP_INCLUDE_DIR");
    println!("cargo:rerun-if-env-changed=WINFSP_LIB_DIR");
    if std::env::var("CARGO_CFG_TARGET_OS").unwrap() != "windows"
        || std::env::var_os("CARGO_FEATURE_WINFSP").is_none()
    {
        return;
    }
    let include = std::env::var_os("WINFSP_INCLUDE_DIR")
        .expect("winfsp feature requires WINFSP_INCLUDE_DIR (WinFsp SDK include)");
    let lib = std::env::var_os("WINFSP_LIB_DIR")
        .expect("winfsp feature requires WINFSP_LIB_DIR (WinFsp SDK lib)");
    assert_eq!(
        std::env::var("CARGO_CFG_TARGET_ARCH").unwrap(),
        "x86_64",
        "WinFsp adapter currently supports x64 only"
    );
    cc::Build::new()
        .file("src/windows/bridge.c")
        .include(include)
        .warnings(true)
        .flag_if_supported("-Wno-unused-parameter")
        .compile("vfs_winfsp_bridge");
    println!(
        "cargo:rustc-link-search=native={}",
        std::path::PathBuf::from(lib).display()
    );
    println!("cargo:rustc-link-lib=winfsp-x64");
    println!("cargo:rustc-link-lib=advapi32");
}
