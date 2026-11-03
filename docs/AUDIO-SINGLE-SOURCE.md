# Single-audio-source policy — 1.6.45

The app has two playback surfaces: the main `<audio>` element and the
SoundCloud/Mixcloud widget `<iframe>`. They must never play together.

- **Embed selected** (`openEmbed`, and again on iframe `load`): `closePlayer()`
  pauses and clears the main audio source, queue/progress, player chrome,
  MediaSession metadata/state and playback heartbeat **before** the iframe src
  is set. This is unconditional because the cross-origin widget cannot be
  observed reliably.
- **Main player starts** (`playItem`, `resolveAndPlay`, `togglePlay`,
  `retryPlayback`): `closeEmbed()` runs first, hiding the panel and removing
  the iframe `src`. A stale iframe `load` event is guarded so it cannot pause a
  newly started main track.
- **Navigation** (`navigate`): closes any active embed; the destination starts
  with no iframe source and a clean main player.
- **Close/backdrop controls** still remove the iframe src as before.
- Does not change embed provider URLs, CSP, downloads or the spinner; the
  existing embed CORS/CSP tests and download/spinner e2e remain green.
