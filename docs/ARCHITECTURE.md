# Architecture & API notes

A sanitized overview of how the TELUS SmartHome+ platform was integrated. No
account identifiers, serials, or credentials are included here.

## Authentication (headless)

TELUS SmartHome+ is a white-label platform (Sweepr/"Area22" tenant). There is
no public API; the mobile app uses a Platform API behind an API key plus a
per-user bearer token.

1. `GET  acr/idpurl` → returns the server-driven login URL (the hard-coded
   auth-gateway host in the app is never called directly).
2. `POST <loginUrl>` with `{ authdomain, loginid, password }` → session cookies.
3. `GET  acr/token` (with those cookies) → `{ accessToken, refreshToken }`.
4. `POST acr/refresh-token` with `{ authCode: <refreshToken> }` → new tokens.

Every Platform call carries:

```
Authorization: Bearer <access token>
x-api-key: <app platform key>
x-unified-household-id: <household id>
x-unified-correlation-id: <uuid per request>
```

> **Token expiry quirk:** several endpoints return **`403` "explicit deny"**
> (not `401`) when the access token has expired. The client refreshes and
> retries on **both** 401 and 403 — without this, polling silently dies and
> every entity goes *Unavailable*.

## Devices

- `GET /presentation-management/household/{householdId}/devices`
  → `{ returnCode, body: { devices: [ … ] } }`.
- Each device carries `smartHomeDeviceMetadata` with an OCF-style `resources[]`
  list (`/pirSensor`, `/aiSensor`, `/maintenance/info`, `/siren`, `/spotlight`,
  …), `connectivityStatus`, manufacturer, model, and serial.
- The camera is `type: "camera"`, a first-party device (`connectorId:
  "1p-camera"`, model `telus.d.outdoorcamera`).

### AI detection

The `/aiSensor` resource carries the latest classified detection:

```jsonc
{
  "href": "/aiSensor",
  "eventName": "Body",            // Body(=Person) | Vehicle | Animal | Package | Motion
  "value": true,                   // actively detecting right now
  "timestamp": 1792167021636,      // epoch ms
  "detectedObject": {
    "module_name": "Motion",
    "confidence": 0,
    "boundingBox": { "topLeft_X": …, "topLeft_Y": …, "bottomRight_X": …, "bottomRight_Y": … },
    "canvasSize": [1440, 1920]
  }
}
```

Detection classes and per-class recording are configured under
`/maintenance/event_trigger_settings` (`AI_ML/{Animal,Motion,Package,Vehicle}`).
The camera also publishes events over **MQTT** (`Action-MQTT = 1`) — the basis
for a future real-time event path.

## Camera live video (KVS WebRTC → RTSP)

- `GET /video-management/video-play/{householdId}/live-communication/devices/{deviceId}?protocols=["WSS","HTTPS"]&role=VIEWER`
  returns `{ channelArn, region, sessionId, client: { resourceEndpointList:
  [{WSS},{HTTPS}], credentials: { AccessKeyId, SecretAccessKey, SessionToken,
  Expiration } } }`. The temporary AWS/STS credentials expire in minutes, so
  this is fetched per live view.
- The stream is **AWS Kinesis Video Streams WebRTC** (region `ca-central-1`),
  signed with SigV4 over a WSS signaling channel (base64 `SDP_OFFER` /
  `SDP_ANSWER` / `ICE_CANDIDATE`).
- Video codec: **H.264 Constrained Baseline** (`profile-level-id=42e01f`,
  packetization-mode=1); audio: Opus 48 kHz stereo.

### Why a bridge, and why GStreamer

`aiortc` completed the KVS handshake (ICE + DTLS) but never received media.
GStreamer's `webrtcbin` (libnice + libwebrtc-grade media) handles ICE
candidate selection and media correctly. The bridge:

```
webrtcbin → rtph264depay → h264parse → (appsink) → ffmpeg -c:v copy → RTSP → MediaMTX → HA
```

No video re-encode (pass-through copy). The bridge is **keyed by device id**,
so one container serves multiple cameras, each on its own MediaMTX path
(`telus_<deviceId>`). A watchdog restarts a session whose video has gone stale
(the camera caps a live session at ~10 minutes), reconnecting with the stored
credentials when they're still valid, else waiting for HA to supply fresh ones.

## Known limits / TODO

- **On-demand**: the bridge should release a camera when no one is viewing
  (free the viewer slot + upload bandwidth) — detect readers via the MediaMTX
  API.
- **Audio** is not muxed into the RTSP output yet.
- **Events** come from the 30 s device poll; MQTT would make them real-time.
- **Clips**: `/video_interval` resources carry `kvsBundle` references to
  recorded clips — not yet exposed.
