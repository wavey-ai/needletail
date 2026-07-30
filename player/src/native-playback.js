const OPUS_MASTER_CODEC_MIME_TYPE = 'audio/mp4; codecs="opus"';
const OPUS_SAMPLE_ENTRY_MIME_TYPE = 'audio/mp4; codecs="Opus"';

/**
 * Select the HLS source that the browser's native media pipeline can decode.
 *
 * Some native HLS implementations treat the RFC 6381 codec token in a master
 * playlist as case-sensitive even though they can decode the registered
 * `Opus` MP4 sample entry. Loading the media playlist in that specific case
 * lets the native pipeline derive the codec from the initialization segment.
 * HLS.js continues to use the master playlist and retain all advertised
 * variants.
 */
export function selectNativePlaylistUrl({
  audioFormat,
  masterPlaylistUrl,
  mediaPlaylistUrl,
  canPlayType,
}) {
  if (audioFormat !== "opus" || typeof canPlayType !== "function") {
    return masterPlaylistUrl;
  }
  const lowerCaseCodecSupported = Boolean(canPlayType(OPUS_MASTER_CODEC_MIME_TYPE));
  const registeredCodecSupported = Boolean(canPlayType(OPUS_SAMPLE_ENTRY_MIME_TYPE));
  return registeredCodecSupported && !lowerCaseCodecSupported
    ? mediaPlaylistUrl
    : masterPlaylistUrl;
}
