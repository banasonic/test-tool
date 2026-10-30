#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::path::PathBuf;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

#[tauri::command]
fn load_config(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let path = app.path().app_config_dir().map_err(|e| e.to_string())?.join("config.json");
    if !path.exists() { return Ok(serde_json::json!({"Default": {}, "global": {"language": "Arabic"}})); }
    let raw = fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(&raw).map_err(|e| e.to_string())
}

#[tauri::command]
fn save_config(app: tauri::AppHandle, config: serde_json::Value) -> Result<(), String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = dir.join("config.json");
    fs::write(path, serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?).map_err(|e| e.to_string())
}

#[tauri::command]
async fn launch_legacy(app: tauri::AppHandle) -> Result<(), String> {
    let cmd = app.shell().sidecar("sftp-legacy").map_err(|e| e.to_string())?;
    cmd.spawn().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn app_config_path(app: tauri::AppHandle) -> Result<String, String> {
    Ok(app.path().app_config_dir().map_err(|e| e.to_string())?.join("config.json").to_string_lossy().to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![load_config, save_config, launch_legacy, app_config_path])
        .setup(|app| {
            let dir = app.path().app_config_dir()?;
            fs::create_dir_all(&dir)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running SFTP Studio");
}
