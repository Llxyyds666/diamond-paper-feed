# Bark Custom Icon Design

## Goal

Give both daily Bark notifications the same recognizable diamond-device icon without weakening the existing secret and endpoint boundaries.

## Asset

- Create one original square PNG at `assets/diamond-bark-icon.png`.
- Use a 512 × 512 composition with a deep-blue background, a cyan faceted diamond, and restrained circuit-trace details.
- Include no text, watermark, third-party logo, or externally licensed artwork.
- Keep the important mark centered and readable at notification-icon size.
- Publish the committed asset through the repository's existing GitHub Pages site at `https://llxyyds666.github.io/diamond-paper-feed/assets/diamond-bark-icon.png`.

## Bark Request Contract

Bark officially supports an `icon` URL in JSON POST requests. Keep the fixed endpoint `POST https://api.day.app/push`; do not append `&icon=` or place the device key in any URL.

Add a non-secret `BARK_ICON_URL` constant to the Bark delivery module and include:

```json
{"icon":"https://llxyyds666.github.io/diamond-paper-feed/assets/diamond-bark-icon.png"}
```

in the JSON body for both planned messages. Existing `device_key`, title, body, group, and optional tap URL behavior remains unchanged.

## Failure and Security Boundaries

- The icon is decorative; it does not change whether a message is attempted or considered successful.
- Continue to make one request per message, reject redirects, validate responses strictly, and sanitize diagnostics.
- Never fetch or validate the icon from the workflow before sending; Bark/iOS performs the remote icon retrieval.
- The public icon URL contains no token or user-specific data.

## Documentation and Tests

- Test that both Bark JSON request bodies contain exactly the configured icon URL while the request endpoint remains the fixed official endpoint.
- Preserve redirect, strict-JSON, independent-message, and credential-leak regression coverage.
- Add an acceptance check that the PNG exists and has the expected dimensions, and document the custom icon URL plus iOS 15+ support in `README.md`.
- Run the full suite and credential scan before integration.

## Release Verification

After the feature branch is integrated and GitHub Pages publishes the asset, verify the icon URL returns a PNG successfully. Then run the normal summary workflow and confirm both Bark notifications display the same custom icon. Record only URLs, run IDs, and statuses—never secret values or Bark request bodies.
