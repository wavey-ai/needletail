use std::fmt;

/// Value-free entropy-source failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EntropyError;

impl fmt::Display for EntropyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("cryptographic entropy is unavailable")
    }
}

impl std::error::Error for EntropyError {}

/// Injected cryptographic entropy boundary used for IDs, nonces, and exchanges.
pub trait EntropySource: Send + Sync {
    /// Fill the complete destination or fail without returning partial entropy.
    ///
    /// # Errors
    ///
    /// Returns [`EntropyError`] when the underlying source is unavailable.
    fn fill_bytes(&self, destination: &mut [u8]) -> std::result::Result<(), EntropyError>;
}

/// Operating-system cryptographic entropy. This type stores no secret state.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemEntropy;

impl EntropySource for SystemEntropy {
    fn fill_bytes(&self, destination: &mut [u8]) -> std::result::Result<(), EntropyError> {
        getrandom::getrandom(destination).map_err(|_error| EntropyError)
    }
}
