# IDOLY PRIDE Account Export

Export account data from the Japanese server: cards, items, outfits, hair, poses, goods, profile settings, progress, gifts, diary read states, and current master data. Photo attributes and skills are always included; photo images and showcases are optional. Supports offline decoding and file integrity checks.
Assess the risks before use.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/), ADB, and [SolisClient](https://github.com/vilebbit/SolisClient). Run `uv sync --locked` in this tool's directory.

## Export

Connect an Android device or emulator, log in normally, and reach the game's home screen. Keep the account idle during export. The device must support `run-as` for the game or have root ADB enabled.

```sh
uv run account_export.py --solis-dir /path/to/SolisClient fetch \
  --device emulator-5560 --adb /path/to/adb \
  --gacha-history --photos --showcases
```

Replace the paths and device serial. Account tokens, the Firebase API key, and the fixed-photo asset catalog are read automatically from the device. Optional flags:

- `--photos`: photo originals and thumbnails, including unclaimed photo records, plus accessible event albums.
- `--showcases`: current layouts, slots, frames, music, preset metadata, and preview images.
- `--gacha-history`: server-retained gacha history.

Omit a flag to skip that optional export. Master data packages are included by default; use `--skip-master-data` to keep only their metadata.

If automatic API key detection fails, use `--firebase-settings /path/to/settings.json` with `{"api_key":"YOUR_FIREBASE_API_KEY"}`, or set the `IDOLY_FIREBASE_API_KEY` environment variable.

To use an authentication file, replace `--device` with `--auth-xml` or `--token-file` and specify `--app-version`. Token files must contain `refresh_token`. Fixed photos then require `--octo-cache /path/to/octocacheevai` or `--octo-manifest /path/to/OctoManifest.json`. See `uv run account_export.py fetch --help` for other options.

## Output and Verification

Exports are saved to `exports/<timestamp>/` under this tool's directory, with category subdirectories. Use `--output` to choose another new directory. Existing directories are never overwritten.

- `summary.json`: account summary; `account.json`: decoded account snapshot.
- `tables/`, `responses/`: categorized data and individual API responses.
- `raw/`, `schema.pb`: raw data and protocol definitions.
- `master/`: encrypted master packages, their index, and protobuf schema; decoding keys and types are in `responses/master.json`.
- `photos/`, `showcases/`: optional media and indexes linking files to account records. Fixed photos include source bundles and extracted PNGs.
- `manifest.json`: coverage, failures, unknown fields, and file hashes.

Verify the latest export without specifying a path:

```sh
uv run account_export.py verify
```

To verify another export, use `uv run account_export.py verify /path/to/export`. For offline decoding, see `uv run account_export.py parse --help`.

## Limitations

- Only currently accessible data is exported. History has server retention limits. Saved preset layouts, battle replays, and the full game asset library are not supported. Event-album downloads require an accessible Shelf and have not been verified against a live album; unavailable albums are recorded as gaps. This archive is not an official offline-app save or a supported import format.
- `complete_with_gaps` means accessible data was saved but known gaps remain; `incomplete` means a requested operation failed. Check `manifest.json` and media indexes. File verification checks saved bytes, not server completeness.
- For `DateChanged (1003)`, log in normally and retry with a new output directory. Protocol definitions must match the game version. Check `manifest.json` for failures and unknown fields.
- Keep exports and authentication files private. Photo URL responses omit download links; authentication tokens stay in memory. Authentication may affect another client session. The script does not import data, change ownership, save layouts, claim rewards, or spend resources.
