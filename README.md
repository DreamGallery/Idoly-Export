# IDOLY PRIDE Account Export

Export resources, items, cards, character and account levels, quest scores, story progress, gifts, and other account data from the Japanese server. Supports offline decoding and file integrity checks.
Assess the risks before use.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/), ADB, and [SolisClient](https://github.com/vilebbit/SolisClient). Run `uv sync --locked` in this tool's directory.

## Export

Connect an Android device or emulator, log in normally, and reach the game's home screen. Keep the account idle during export. The device must support `run-as` for the game or have root ADB enabled.

```sh
uv run account_export.py --solis-dir /path/to/SolisClient fetch \
  --device emulator-5560 --adb /path/to/adb \
  --gacha-history
```

Replace the example paths and device serial. The script reads the account token and Firebase API key automatically. The optional `--gacha-history` flag includes gacha history.

If automatic API key detection fails, use `--firebase-settings /path/to/settings.json` with `{"api_key":"YOUR_FIREBASE_API_KEY"}`, or set the `IDOLY_FIREBASE_API_KEY` environment variable.

To use an existing authentication file, replace `--device` with `--auth-xml` or `--token-file` and specify `--app-version`. Token files must contain `refresh_token`. See `uv run account_export.py fetch --help` for other options.

## Output and Verification

Exports are saved to `exports/<timestamp>/` under this tool's directory, with category subdirectories. Use `--output` to choose another new directory. Existing directories are never overwritten.

- `summary.json`: account summary; `account.json`: decoded account snapshot.
- `tables/`, `responses/`: categorized data and individual API responses.
- `raw/`, `schema.pb`: raw data and protocol definitions.
- `manifest.json`: coverage, failures, unknown fields, and file hashes.

Verify the latest export without specifying a path:

```sh
uv run account_export.py verify
```

To verify another export, use `uv run account_export.py verify /path/to/export`. For offline decoding, see `uv run account_export.py parse --help`.

## Limitations

- Only currently accessible data is exported. History is limited by server retention. Media files such as original photos, some event-specific data, and battle replays are not included. Successful queries or integrity checks do not establish a complete server backup.
- For `DateChanged (1003)`, log in normally and retry with a new output directory. Protocol definitions must match the game version. Check `manifest.json` for failures and unknown fields.
- Export and authentication files contain private data. Authentication may affect another client session. The script does not claim rewards, perform gacha draws, or spend resources.
