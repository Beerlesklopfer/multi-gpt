//! Lokale Freigabe für das FLUX-Profil. Noch kein Start- oder Generierungsweg.
//!
//! Alle Angaben stammen aus der Prozessumgebung des Rechners, niemals aus einem
//! Serverrahmen. Ein Image für FLUX wird nicht in `Config.images` aufgenommen:
//! Coding-Sitzungen bekommen dadurch weder GPU- noch Volume-Zugriff.

use std::collections::HashMap;

use crate::config::ConfigError;
use crate::protocol;

const SETTINGS: [&str; 4] = [
    "MGPT_FLUX_IMAGE",
    "MGPT_FLUX_RENDER_DEVICE",
    "MGPT_FLUX_MODELS_VOLUME",
    "MGPT_FLUX_CACHE_VOLUME",
];

/// Freigabe eines AMD-GPU-Profils. Die tatsächliche Geräte-/Laufzeitprüfung folgt
/// beim Start; gültige Einstellungen allein bestätigen keine GPU-Unterstützung.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FluxConfig {
    pub image: String,
    /// Auswahl aus der Ersteinrichtung; ohne Auswahl kein GPU-Start.
    /// Genau ein Render-Gerät. `/dev/kfd` folgt in der festen GPU-Vorlage.
    pub render_device: Option<String>,
    /// Podman-Namen, keine Hostpfade; Gewichte werden später read-only eingebunden.
    pub models_volume: String,
    pub cache_volume: String,
}

impl FluxConfig {
    /// Ohne FLUX-Einstellungen deaktiviert. Image und Volumes sind erforderlich;
    /// die GPU-Auswahl darf bis zum Abschluss der Ersteinrichtung fehlen.
    pub fn from_env(env: &HashMap<String, String>) -> Result<Option<Self>, ConfigError> {
        let values = SETTINGS.map(|name| {
            env.get(name)
                .map(|value| value.trim())
                .filter(|value| !value.is_empty())
        });
        if values.iter().all(Option::is_none) {
            return Ok(None);
        }
        for (index, (name, value)) in SETTINGS.iter().zip(values).enumerate() {
            if index != 1 && value.is_none() {
                return Err(ConfigError(format!("{name} fehlt für das FLUX-Profil.")));
            }
        }
        let [
            Some(image),
            render_device,
            Some(models_volume),
            Some(cache_volume),
        ] = values
        else {
            unreachable!("Image und Volumes wurden geprüft")
        };
        if !protocol::IMAGE_RE.is_match(image) {
            return Err(ConfigError(
                "MGPT_FLUX_IMAGE: Image mit SHA-256-Digest erforderlich.".into(),
            ));
        }
        let render_number = render_device
            .and_then(|device| device.strip_prefix("/dev/dri/renderD"))
            .filter(|suffix| !suffix.is_empty() && suffix.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|suffix| suffix.parse::<u32>().ok());
        if render_device.is_some() && !matches!(render_number, Some(128..)) {
            return Err(ConfigError(
                "MGPT_FLUX_RENDER_DEVICE: genau ein /dev/dri/renderD<N> ab renderD128 erforderlich."
                    .into(),
            ));
        }
        for (name, value) in [(SETTINGS[2], models_volume), (SETTINGS[3], cache_volume)] {
            // Bewusst nur benannte Volumes: keine Mount-Optionen oder Pfade.
            if value.len() > 128
                || !value.as_bytes()[0].is_ascii_alphanumeric()
                || !value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            {
                return Err(ConfigError(format!(
                    "{name}: benannter Podman-Speicher erforderlich, kein Pfad oder Mount-Ausdruck."
                )));
            }
        }
        if models_volume == cache_volume {
            return Err(ConfigError(
                "FLUX: Modell- und Cache-Volume müssen verschieden sein.".into(),
            ));
        }
        Ok(Some(Self {
            image: image.into(),
            render_device: render_device.map(str::to_owned),
            models_volume: models_volume.into(),
            cache_volume: cache_volume.into(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    fn settings() -> HashMap<String, String> {
        HashMap::from([
            ("MGPT_URL".into(), "https://mgpt.example".into()),
            (
                SETTINGS[0].into(),
                format!("localhost/flux:1@sha256:{}", "a".repeat(64)),
            ),
            (SETTINGS[1].into(), "/dev/dri/renderD128".into()),
            (SETTINGS[2].into(), "mgpt-flux-models".into()),
            (SETTINGS[3].into(), "mgpt-flux-cache".into()),
        ])
    }

    #[test]
    fn local_opt_in_does_not_authorize_coding_images() {
        assert!(Config::new("wss://mgpt.example/runner/ws").flux.is_none());
        assert_eq!(FluxConfig::from_env(&HashMap::new()).unwrap(), None);
        let config = Config::from_env(&settings()).unwrap();
        let flux = config.flux.unwrap();
        assert!(config.images.is_empty());
        assert_eq!(flux.render_device.as_deref(), Some("/dev/dri/renderD128"));
        assert_eq!(flux.models_volume, "mgpt-flux-models");
    }

    #[test]
    fn first_run_requires_local_gpu_selection_without_a_default_device() {
        let mut env = settings();
        env.remove(SETTINGS[1]);
        let config = Config::from_env(&env).unwrap();
        assert!(config.flux.unwrap().render_device.is_none());
    }

    #[test]
    fn incomplete_settings_fail_at_runner_start() {
        for key in [SETTINGS[0], SETTINGS[2], SETTINGS[3]] {
            let mut env = settings();
            env.remove(key);
            assert!(Config::from_env(&env).unwrap_err().0.contains(key));
        }
    }

    #[test]
    fn rejects_unpinned_images_host_paths_and_broad_device_access() {
        for (key, value) in [
            (SETTINGS[0], "localhost/flux:latest"),
            (SETTINGS[1], "/dev/dri"),
            (SETTINGS[1], "/dev/fb0"),
            (SETTINGS[1], "/dev/dri/renderD128/../card0"),
            (SETTINGS[1], "/dev/dri/renderD127"),
            (SETTINGS[2], "/home/user/models"),
            (SETTINGS[2], "models:/models:rw"),
            (SETTINGS[3], "--privileged"),
            (SETTINGS[3], "mgpt-flux-models"),
        ] {
            let mut env = settings();
            env.insert(key.into(), value.into());
            assert!(Config::from_env(&env).is_err(), "{key}={value}");
        }
    }
}
