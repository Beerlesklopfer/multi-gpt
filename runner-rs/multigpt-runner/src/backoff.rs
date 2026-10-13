//! Wartezeiten zwischen Verbindungsversuchen (wie `runner_agent/backoff.py`).

use std::time::Duration;

/// Zufallsquelle für die Streuung: Werte in `[0, 1)`; im Test ersetzbar.
pub type RandFn = Box<dyn FnMut() -> f64 + Send + Sync>;

/// Exponentielles Backoff mit Streuung.
///
/// Nach [`Backoff::reset`] (stabile Verbindung) beginnt es wieder bei `base`.
/// Close-Codes außerhalb von `RECONNECT` warten gleich die längste Zeit.
pub struct Backoff {
    pub base: f64,
    pub factor: f64,
    pub maximum: f64,
    pub jitter: f64,
    pub attempt: u32,
    rand: RandFn,
}

impl std::fmt::Debug for Backoff {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Backoff")
            .field("base", &self.base)
            .field("factor", &self.factor)
            .field("maximum", &self.maximum)
            .field("jitter", &self.jitter)
            .field("attempt", &self.attempt)
            .finish()
    }
}

impl Default for Backoff {
    fn default() -> Self {
        Self::new(1.0, 2.0, 60.0, 0.2, Box::new(rand::random::<f64>))
    }
}

impl Backoff {
    pub fn new(base: f64, factor: f64, maximum: f64, jitter: f64, rand: RandFn) -> Self {
        Self {
            base,
            factor,
            maximum,
            jitter,
            attempt: 0,
            rand,
        }
    }

    /// Backoff mit fester Zufallsquelle (Tests).
    pub fn with_rand(rand: RandFn) -> Self {
        Self {
            rand,
            ..Self::default()
        }
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
    }

    /// Nächste Wartezeit in Sekunden.
    pub fn next_secs(&mut self, longest: bool) -> f64 {
        let delay = if longest {
            self.maximum
        } else {
            (self.base * self.factor.powi(self.attempt.min(1000) as i32)).min(self.maximum)
        };
        self.attempt = self.attempt.saturating_add(1);
        let spread = delay * self.jitter;
        (delay - spread + 2.0 * spread * (self.rand)()).max(0.0)
    }

    /// Nächste Wartezeit.
    pub fn next_delay(&mut self, longest: bool) -> Duration {
        Duration::from_secs_f64(self.next_secs(longest))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grows_and_caps() {
        let mut b = Backoff::with_rand(Box::new(|| 0.5));
        let delays: Vec<f64> = (0..8).map(|_| b.next_secs(false)).collect();
        assert_eq!(delays, vec![1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]);
        b.reset();
        assert_eq!(b.next_secs(false), 1.0);
        assert_eq!(b.next_secs(true), 60.0);
    }

    #[test]
    fn jitter_bounds() {
        let mut low = Backoff::with_rand(Box::new(|| 0.0));
        let mut high = Backoff::with_rand(Box::new(|| 0.999_999));
        assert!((low.next_secs(false) - 0.8).abs() < 1e-9);
        assert!(high.next_secs(false) < 1.2);
    }
}
