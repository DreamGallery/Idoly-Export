#!/usr/bin/env python3
"""Export IDOLY PRIDE account snapshots without invoking gameplay mutations."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
EXPORTS_ROOT = ROOT / 'exports'
PACKAGE = 'game.qualiarts.idolypride'
API_HOST = 'api.game-idolypride.jp:443'
PHOTO_BUCKETS = ('udc-idolypride', 'event-idolypride')
CATALOG_QUERIES = ('Quest.ListMain', 'Quest.ListDaily', 'Quest.ListContest',
                   'Quest.ListTower', 'Quest.TowerTop', 'Story.ListEvent', 'Story.ListExtra',
                   'Diary.GetInfos')


class ExportError(Exception):
    """A diagnostic containing no credentials or server response text."""

    def __init__(self, message, *, application_codes=(), http_status=None):
        super().__init__(message)
        self.application_codes = tuple(application_codes)
        self.http_status = http_status


def file_hash(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ExportError('Cannot read the specified JSON file.') from None


def load_protocol(directory=None, descriptor_set=None):
    if descriptor_set:
        from google.protobuf.descriptor_pb2 import FileDescriptorSet
        from google.protobuf.descriptor_pool import DescriptorPool
        from google.protobuf.message_factory import GetMessageClass
        from types import SimpleNamespace
        definitions = FileDescriptorSet.FromString(Path(descriptor_set).read_bytes())
        pool = DescriptorPool()
        for definition in definitions.file:
            pool.Add(definition)
        descriptor = pool.FindMessageTypeByName('api.UserGetResponse')
        return SimpleNamespace(DESCRIPTOR=descriptor.file, UserGetResponse=GetMessageClass(descriptor))
    if directory is None:
        raise ExportError('Provide --solis-dir, or --descriptor-set for offline parsing.')
    directory = Path(directory).resolve()
    if not (directory / 'papi_pb2.py').is_file():
        raise ExportError('Protocol directory must contain papi_pb2.py and its dependencies.')
    sys.path.insert(0, str(directory))
    try:
        api = importlib.import_module('papi_pb2')
    except (ImportError, RuntimeError, TypeError):
        raise ExportError('Cannot load protocol; install requirements and check generated protobuf version.') from None
    return api


def as_dict(message):
    from google.protobuf.json_format import MessageToDict
    return MessageToDict(message, preserving_proto_field_name=True,
                         use_integers_for_enums=True,
                         always_print_fields_with_no_presence=True)


def unknown_fields(message, path='$'):
    """Report unknown wire fields recursively, without exposing their values."""
    from google.protobuf.unknown_fields import UnknownFieldSet
    result = []
    for field in UnknownFieldSet(message):
        result.append({'path': path, 'field_number': field.field_number,
                       'wire_type': field.wire_type})
    for field, value in message.ListFields():
        if field.type != field.TYPE_MESSAGE:
            continue
        next_path = path + '.' + field.name
        if field.label == field.LABEL_REPEATED:
            if field.message_type.GetOptions().map_entry:
                subfield = field.message_type.fields_by_name['value']
                if subfield.type == subfield.TYPE_MESSAGE:
                    for key, child in value.items():
                        result.extend(unknown_fields(child, next_path + '[' + str(key) + ']'))
            else:
                for index, child in enumerate(value):
                    result.extend(unknown_fields(child, f'{next_path}[{index}]'))
        else:
            result.extend(unknown_fields(value, next_path))
    return result


def save_schema(api, output):
    from google.protobuf.descriptor_pb2 import FileDescriptorSet
    result, seen = FileDescriptorSet(), set()
    def add(descriptor):
        if descriptor.name in seen:
            return
        seen.add(descriptor.name)
        for dependency in descriptor.dependencies:
            add(dependency)
        descriptor.CopyToProto(result.file.add())
    add(api.DESCRIPTOR)
    write_bytes(output / 'schema.pb', result.SerializeToString())


def field_schema(descriptor):
    return [{'name': f.name, 'number': f.number,
             'repeated': f.label == f.LABEL_REPEATED,
             'type': f.message_type.full_name if f.message_type else
                     f.enum_type.full_name if f.enum_type else f.type,
             'fields': field_schema(f.message_type) if f.message_type else []}
            for f in descriptor.fields]


class Archive:
    def __init__(self, output, api, source):
        self.output = Path(output).resolve()
        if self.output.exists():
            raise ExportError('Output directory already exists; choose a new directory to preserve earlier exports.')
        self.output.mkdir(parents=True, mode=0o700)
        self.api = api
        self.manifest = {'format_version': 2, 'started_at': datetime.now(timezone.utc).isoformat(),
                         'source': source, 'status': 'in_progress', 'responses': [], 'coverage': {},
                         'limitations': ['Current server-visible records, not a lifetime acquisition ledger.',
                                         'Separate requests are not an atomic server snapshot.',
                                         'Photo and showcase media require their optional export flags.',
                                         'Not an offline-app save or a backup of all game assets.',
                                         'Event modes and other services are not implied by User.Get coverage.']}
        save_schema(api, self.output)
        self.save()

    def save(self):
        write_json(self.output / 'manifest.json', self.manifest)

    def response(self, name, message, raw, *, redacted=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ExportError('Invalid archive entry name.')
        # URL responses may contain download credentials; retain only their inventory.
        if redacted is None:
            write_bytes(self.output / 'raw' / (name + '.pb'), raw)
        write_json(self.output / 'responses' / (name + '.json'),
                   as_dict(message) if redacted is None else redacted)
        entry = {'name': name, 'message_type': message.DESCRIPTOR.full_name,
                 'captured_at': datetime.now(timezone.utc).isoformat(),
                 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                 'unknown_fields': unknown_fields(message)}
        if redacted is not None:
            entry.update(raw_stored=False, redaction='Download URLs omitted; hash describes the original response.')
        self.manifest['responses'].append(entry)
        self.save()
        return entry

    def account(self, message, raw):
        if message.DESCRIPTOR.full_name != 'api.UserGetResponse':
            raise ExportError('Expected UserGetResponse; incremental CommonResponse is not a full snapshot.')
        if not message.HasField('user') or not message.HasField('userPublic') or not message.userPublic.serverUserId:
            raise ExportError('Response lacks user/account identity; not a valid full account snapshot.')
        entry = self.response('user', message, raw)
        data = as_dict(message)
        coverage = {}
        for field in message.DESCRIPTOR.fields:
            repeated = field.label == field.LABEL_REPEATED
            present = True if repeated or not field.has_presence else message.HasField(field.name)
            value = data.get(field.name, None)
            count = len(getattr(message, field.name)) if repeated else (1 if present else 0)
            coverage[field.name] = {'records': count, 'present': present,
                                    'field_number': field.number, 'repeated': repeated}
            data.setdefault(field.name, value)
            write_json(self.output / 'tables' / (field.name + '.json'), value)
        write_json(self.output / 'account.json', data)
        write_json(self.output / 'schema.json', field_schema(message.DESCRIPTOR))
        self.manifest['coverage']['user'] = {
            'status': 'decoded_with_unknown_fields' if entry['unknown_fields'] else 'all_known_fields_decoded',
            'field_count': len(coverage), 'fields': coverage,
            'server_completeness': 'User.Get response; no pagination in current protocol'}
        summary = {key: data.get(key) for key in ('user', 'balance', 'totalCount', 'hierarchy')}
        summary['record_counts'] = {key: value['records'] for key, value in coverage.items()}
        write_json(self.output / 'summary.json', summary)
        self.save()

    def finish(self, status='complete_for_requested_scope'):
        self.manifest['status'] = status
        self.manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.manifest['files'] = {str(path.relative_to(self.output)): file_hash(path)
                                  for path in sorted(self.output.rglob('*'))
                                  if path.is_file() and path.name != 'manifest.json'}
        self.save()


def export_gifts(fetch_page, archive, max_pages=1000):
    """No deduplication: duplicate IDs/count drift are evidence of an unstable list."""
    gifts, seen, total, offset = [], set(), None, 0
    for index in range(max_pages):
        message, raw = fetch_page(offset)
        archive.response(f'gifts-{index:04d}', message, raw)
        if total is None:
            total = message.count
        if message.count != total:
            raise ExportError('Gift total changed during pagination; repeat export when account is idle.')
        page = as_dict(message).get('gifts', [])
        for record in page:
            identity = record.get('giftId')
            if not identity or identity in seen:
                raise ExportError('Gift page contains a missing or repeated ID; pagination is incomplete.')
            seen.add(identity)
        gifts.extend(page)
        if not message.hasNext:
            if len(gifts) != total:
                raise ExportError('Gift count does not match returned records; export remains incomplete.')
            write_json(archive.output / 'tables' / 'gifts.json', gifts)
            archive.manifest['coverage']['gifts'] = {'status': 'all_pages_returned', 'records': len(gifts),
                                                    'server_count': total, 'pages': index + 1}
            archive.save()
            return gifts
        if not page:
            raise ExportError('Gift pagination returned an empty page with hasNext=true.')
        offset += len(page)
    raise ExportError('Gift page limit reached; export remains incomplete.')


class Downloader:
    """Stream bounded HTTPS downloads; never log URLs or forward credentials."""
    def __init__(self):
        import requests
        self.requests = requests
        self.session = requests.Session()

    def close(self):
        self.session.close()

    def get(self, url, destination, *, firebase_token=None, size=None, md5=None):
        parts = urlsplit(url)
        host = parts.hostname or ''
        allowed = host in ('climaster.game-idolypride.jp', 'firebasestorage.googleapis.com') or host.endswith('.cloudfront.net')
        if (parts.scheme != 'https' or not allowed or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment):
            raise ExportError('Download URL is not on an allowed HTTPS host.')
        if firebase_token and (host != 'firebasestorage.googleapis.com'
                               or not any(parts.path.startswith(f'/v0/b/{bucket}/o/') for bucket in PHOTO_BUCKETS)):
            raise ExportError('Firebase credentials cannot be sent to this destination.')
        limit = 128 * 1024 * 1024
        if size is not None and not 0 < size <= limit:
            raise ExportError('Download size is outside the supported range.')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        headers = {'Authorization': 'Firebase ' + firebase_token} if firebase_token else {}
        for attempt in range(3):
            fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    with self.session.get(url, headers=headers, stream=True, timeout=(15, 60),
                                          allow_redirects=False) as response:
                        if response.status_code != 200:
                            raise ExportError(f'Download rejected (HTTP {response.status_code}).',
                                              http_status=response.status_code)
                        count = 0
                        for chunk in response.iter_content(1024 * 1024):
                            count += len(chunk)
                            if count > limit or (size is not None and count > size):
                                raise ExportError('Download exceeded its expected size.')
                            stream.write(chunk)
                if not count or (size is not None and count != size):
                    raise ExportError('Downloaded size does not match the resource catalog.')
                if md5 is not None and file_hash(temporary, 'md5') != md5.lower():
                    raise ExportError('Downloaded MD5 does not match the resource catalog.')
                os.replace(temporary, destination)
                return
            except (self.requests.RequestException, ExportError) as error:
                retry = isinstance(error, self.requests.RequestException) or error.http_status in (429, 500, 502, 503, 504)
                if not retry or attempt == 2:
                    if isinstance(error, ExportError):
                        raise
                    raise ExportError('Download transport failed after bounded retries.') from None
                time.sleep(attempt + 1)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def safe_failure(error):
    return str(error) if isinstance(error, ExportError) else type(error).__name__


def media_key(kind, asset_id):
    return hashlib.sha256(json.dumps([kind, asset_id], ensure_ascii=False).encode()).hexdigest()


def image_info(path, root):
    from PIL import Image
    try:
        with Image.open(path) as image:
            dimensions, image_format = image.size, image.format
            image.verify()
        with Image.open(path) as image:
            image.load()
        extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(image_format)
        if not extension:
            raise ValueError('Unsupported image format')
    except Exception:
        raise ExportError('Downloaded image could not be decoded and verified.') from None
    final = Path(path).with_suffix(extension)
    if final != path:
        os.replace(path, final)
    return {'path': str(final.relative_to(root)), 'width': dimensions[0], 'height': dimensions[1],
            'format': image_format, 'bytes': final.stat().st_size, 'sha256': file_hash(final)}


def export_master(master, raw, archive, downloader, skip_packages=False):
    archive.response('master', master, raw)
    save_schema(importlib.import_module('pmaster_pb2'), archive.output / 'master')
    archive.manifest['coverage']['master'] = {'status': 'server_returned_response'}
    entries, seen = [], set()
    for index, pack in enumerate(master.masterTag.masterTagPacks):
        entry = {'type': pack.type, 'file_name': pack.fileName, 'expected_bytes': pack.fileSize}
        entries.append(entry)
        if skip_packages:
            entry['status'] = 'not_requested'
            continue
        try:
            if not pack.type or pack.type in seen:
                raise ExportError('Master package type is missing or repeated.')
            seen.add(pack.type)
            path = archive.output / 'master' / 'packages' / f'{index:03d}.pack'
            downloader.get(pack.downloadUrl, path, size=pack.fileSize)
            entry.update(status='downloaded', path=str(path.relative_to(archive.output)))
        except Exception as error:
            entry.update(status='failed', reason=safe_failure(error))
        if (index + 1) % 50 == 0:
            print(f'Master packages: {index + 1}/{len(master.masterTag.masterTagPacks)}', file=sys.stderr)
    write_json(archive.output / 'master' / 'index.json', entries)
    failed = sum(e['status'] == 'failed' for e in entries)
    archive.manifest['coverage']['master_data'] = {
        'status': 'not_requested' if skip_packages else 'failed' if failed or not entries else 'all_packages_downloaded',
        'packages': len(entries), 'failed_packages': failed,
        'encoding': 'Original encrypted packages; Master.Get retains keys and type mapping for later decoding.'}
    archive.save()


def load_octo(args, archive):
    """Reuse SolisClient's cache decoder; no Octo API authentication is required."""
    if args.octo_manifest:
        data = read_json(args.octo_manifest)
    else:
        helpers = importlib.import_module('octo_manager')
        if args.octo_cache:
            candidates = [Path(args.octo_cache).read_bytes()]
        elif args.device:
            try:
                names = adb_output(args.adb, args.device, 'shell', 'run-as', args.package,
                                   'find', 'files', 'cache', '-name', 'octocacheevai', '-type', 'f').decode().splitlines()
                prefix, parent = ('exec-out', 'run-as', args.package, 'cat'), ''
            except ExportError:
                if adb_output(args.adb, args.device, 'shell', 'id', '-u').strip() != b'0':
                    raise ExportError('Cannot read the asset cache; provide --octo-cache or --octo-manifest.') from None
                parent = '/data/user/0/' + args.package + '/'
                names = adb_output(args.adb, args.device, 'shell', 'find', parent + 'files', parent + 'cache',
                                   '-name', 'octocacheevai', '-type', 'f').decode().splitlines()
                prefix = ('exec-out', 'cat')
                names = [name.removeprefix(parent) for name in names]
            names = [name for name in names if re.fullmatch(r'(files|cache)/[A-Za-z0-9_./-]+/octocacheevai', name)
                     and '..' not in Path(name).parts]
            if not names or len(names) > 16:
                raise ExportError('No unambiguous asset cache found; open the game or provide --octo-cache/--octo-manifest.')
            candidates = [adb_output(args.adb, args.device, *prefix, parent + name) for name in names]
        else:
            raise ExportError('Fixed photos require --device, --octo-cache, or --octo-manifest.')
        decoded = []
        for raw in candidates:
            try:
                decoded.append((as_dict(helpers._decrypt_file_database(raw)), raw))
            except Exception:
                continue
        if not decoded:
            raise ExportError('Cannot decode the asset cache with this SolisClient version.')
        data, raw = max(decoded, key=lambda item: int(item[0].get('revision', 0)))
        write_bytes(archive.output / 'photos' / 'catalog.octo', raw)
    if not isinstance(data, dict) or not isinstance(data.get('assetBundleList'), list) or not data.get('urlFormat'):
        raise ExportError('Asset catalog must contain assetBundleList and urlFormat.')
    write_json(archive.output / 'photos' / 'catalog.json', data)
    return data


class PhotoExporter:
    def __init__(self, args, archive, client, downloader, record, api_key, account):
        self.args, self.archive, self.client, self.downloader = args, archive, client, downloader
        self.record, self.api_key = record, api_key
        self.owner = account.get('userPublic', {}).get('publicUserId', '')
        self.catalog = None
        self.catalog_error = None
        self.catalog_entries = {}

    def firebase_image(self, asset_id, variant, base, bucket='udc-idolypride'):
        if not self.owner or not asset_id or '/' in asset_id or '/' in self.owner:
            raise ExportError('Photo owner or asset ID is missing or invalid.')
        if bucket not in PHOTO_BUCKETS:
            raise ExportError('Unsupported photo storage bucket.')
        object_name = f'Photo/{self.owner}/{asset_id}' + ('_t' if variant == 'thumbnail' else '')
        url = f'https://firebasestorage.googleapis.com/v0/b/{bucket}/o/' + quote(object_name, safe='') + '?alt=media'
        path = base / (variant + '.image')
        for attempt in range(2):
            token = self.client.token(self.record, self.api_key, force=bool(attempt))
            try:
                self.downloader.get(url, path, firebase_token=token)
                return image_info(path, self.archive.output)
            except ExportError as error:
                if error.http_status != 401 or attempt:
                    raise

    def fixed_images(self, asset_id, base):
        if self.catalog_error:
            raise ExportError(self.catalog_error)
        if self.catalog is None:
            try:
                self.catalog = load_octo(self.args, self.archive)
                for item in self.catalog['assetBundleList']:
                    if item.get('state') != 4:
                        self.catalog_entries.setdefault(item['name'], []).append(item)
            except Exception as error:
                self.catalog_error = safe_failure(error)
                raise ExportError(self.catalog_error) from None
        name = 'img_photo_full_' + asset_id
        matches = self.catalog_entries.get(name, [])
        if len(matches) != 1:
            raise ExportError('Fixed photo is absent or ambiguous in the asset catalog; refresh the game cache.')
        item = matches[0]
        url = self.catalog['urlFormat']
        for key, value in {'{v}': item['uploadVersionId'], '{type}': 'assetbundle',
                           '{o}': item['objectName'], '{g}': item['generation']}.items():
            url = url.replace(key, str(value))
        if '{' in url or not re.fullmatch('[0-9a-fA-F]{32}', item['md5']):
            raise ExportError('Invalid asset catalog URL template or checksum.')
        bundle = base / 'source.octo'
        self.downloader.get(url, bundle, size=int(item['size']), md5=item['md5'])
        raw = bundle.read_bytes()
        helpers = importlib.import_module('octo_manager')
        decoded = raw if raw.startswith(b'Unity') else helpers.crypt_by_string(raw, name, 0, 0, min(256, len(raw)))
        if not decoded.startswith(b'Unity'):
            raise ExportError('Fixed photo bundle header could not be decoded.')
        import UnityPy
        import UnityPy.config
        UnityPy.config.FALLBACK_UNITY_VERSION = self.args.unity_version
        write_bytes(base / 'source.unity3d', decoded)
        images = []
        for obj in UnityPy.load(decoded).objects:
            if obj.type.name == 'Texture2D':
                texture = obj.read()
                buffer = io.BytesIO()
                texture.image.save(buffer, format='PNG')
                path = base / f'original-{len(images):02d}.png'
                write_bytes(path, buffer.getvalue())
                images.append(image_info(path, self.archive.output))
        if not images:
            raise ExportError('Fixed photo bundle contains no decoded textures.')
        return {'originals': images, 'bundle': str(bundle.relative_to(self.archive.output)),
                'thumbnail': {'status': 'not_applicable', 'reason': 'Original textures extracted from the fixed-photo bundle.'}}

    def export(self, account):
        references, assets, urls = [], {}, {}
        for source, rows in (('photos', account.get('photos', [])),
                             ('photoReport.photos', (account.get('photoReport') or {}).get('photos', []))):
            for index, photo in enumerate(rows):
                key = media_key(photo.get('imageType'), photo.get('assetId'))
                references.append({'source': source, 'index': index, 'photo': photo, 'media_key': key})
                assets.setdefault(key, {'asset_id': photo.get('assetId'), 'image_type': photo.get('imageType')})
        generated = [a['asset_id'] for a in assets.values() if a['image_type'] == 3 and a['asset_id']]
        url_failures = {}
        for start in range(0, len(generated), 50):
            batch = generated[start:start + 50]
            try:
                response, raw = self.client.call('Photo.GetImageURL', self.client.api.PhotoGetImageUrlRequest(type=3, assetIds=batch))
                self.archive.response(f'photo-urls-{start // 50:04d}', response, raw,
                    redacted={'urlList': [{'assetId': row.assetId, 'has_original': bool(row.origUrl),
                                         'has_thumbnail': bool(row.tmbUrl)} for row in response.urlList]})
                if len({row.assetId for row in response.urlList}) != len(response.urlList):
                    raise ExportError('Photo URL response has duplicate asset IDs.')
                urls.update({row.assetId: (row.origUrl, row.tmbUrl) for row in response.urlList if row.assetId in batch})
            except Exception as error:
                url_failures.update({asset: safe_failure(error) for asset in batch})
        index_path = self.archive.output / 'photos' / 'index.json'
        write_json(index_path, {'records': references, 'assets': assets})
        for index, (key, entry) in enumerate(assets.items()):
            base = self.archive.output / 'photos' / 'media' / key
            try:
                if not entry['asset_id']:
                    raise ExportError('Photo record has no asset ID.')
                kind = entry['image_type']
                if kind == 1:
                    entry.update(self.fixed_images(entry['asset_id'], base), status='downloaded')
                elif kind in (2, 3):
                    entry['status'] = 'downloaded'
                    for i, variant in enumerate(('original', 'thumbnail')):
                        try:
                            if kind == 2:
                                entry[variant] = self.firebase_image(entry['asset_id'], variant, base)
                            else:
                                if entry['asset_id'] in url_failures:
                                    raise ExportError(url_failures[entry['asset_id']])
                                url = urls.get(entry['asset_id'], ('', ''))[i]
                                if not url:
                                    raise ExportError('Photo URL response omitted this image variant.')
                                path = base / (variant + '.image')
                                self.downloader.get(url, path)
                                entry[variant] = image_info(path, self.archive.output)
                        except Exception as error:
                            entry['status'] = 'failed'
                            entry[variant] = {'status': 'failed', 'reason': safe_failure(error)}
                else:
                    raise ExportError('Unsupported photo image type; metadata retained.')
            except Exception as error:
                entry.update(status='failed', reason=safe_failure(error))
            write_json(index_path, {'records': references, 'assets': assets})
            if (index + 1) % 25 == 0 or index + 1 == len(assets):
                print(f'Photo assets: {index + 1}/{len(assets)}', file=sys.stderr)
        failed = sum(e['status'] == 'failed' for e in assets.values())
        self.archive.manifest['coverage']['photos'] = {
            'status': 'failed' if failed else 'all_referenced_assets_downloaded',
            'records': len(references), 'unique_assets': len(assets), 'failed_assets': failed,
            'catalog_revision': self.catalog.get('revision') if self.catalog else None}
        self.archive.save()


def export_showcases(client, archive, account, photos):
    from google.protobuf.empty_pb2 import Empty
    response, raw = client.call('Showcase.Top', Empty())
    archive.response('showcase-top', response, raw)
    index = {'slots': as_dict(response)['showcaseInfos'], 'showcases': account.get('showcases', []),
             'presets': account.get('showcaseMysets', []), 'previews': {}}
    failed = 0
    for row in index['showcases']:
        asset_id = row.get('thumbnailAssetId')
        if not asset_id:
            continue
        key = media_key('showcase', asset_id)
        if key in index['previews']:
            continue
        entry = {'asset_id': asset_id, 'status': 'downloaded'}
        index['previews'][key] = entry
        for variant in ('original', 'thumbnail'):
            try:
                entry[variant] = photos.firebase_image(asset_id, variant, archive.output / 'showcases' / 'media' / key)
            except Exception as error:
                entry[variant] = {'status': 'failed', 'reason': safe_failure(error)}
                entry['status'] = 'failed'
        failed += entry['status'] == 'failed'
    saved_presets = sum(bool(row.get('isSave')) for row in index['presets'])
    index['preset_layouts'] = {'status': 'unavailable' if saved_presets else 'no_saved_presets',
                              'saved_presets': saved_presets,
                              'reason': 'ApplyMyset is not in the read-only allowlist; only preset metadata is exported.'}
    write_json(archive.output / 'showcases' / 'index.json', index)
    archive.manifest['coverage']['showcases'] = {
        'status': 'failed' if failed else 'complete_with_gaps' if saved_presets else 'all_current_slots_exported',
        'slots': len(index['slots']), 'previews': len(index['previews']), 'failed_previews': failed,
        'saved_preset_layouts': index['preset_layouts']}
    archive.save()


def export_albums(client, archive, photos):
    """Archive accessible event albums without creating, claiming, or editing photos."""
    from google.protobuf.empty_pb2 import Empty
    coverage = {'status': 'pending', 'albums': []}
    assets = {}
    archive.manifest['coverage']['event_albums'] = coverage
    try:
        top, raw = client.call('Shelf.Top', Empty())
        archive.response('shelf-top', top, raw)
    except ExportError as error:
        if '3024' not in error.application_codes:
            raise
        coverage.update(status='complete_with_gaps', reason='Shelf unavailable (EventConditionInvalid 3024); historical albums cannot be enumerated.')
        write_json(archive.output / 'photos' / 'event-albums.json', coverage)
        archive.save()
        return
    for i, album in enumerate(top.shelfInfo.albumInfos):
        entry = {'album_id': album.albumId, 'unlocked': album.isUnlocked}
        coverage['albums'].append(entry)
        if not album.isUnlocked:
            entry['status'] = 'locked'
            continue
        try:
            response, raw = client.call('Shelf.AlbumTop', client.api.ShelfAlbumTopRequest(shelfAlbumId=album.albumId))
            archive.response(f'shelf-album-{i:04d}', response, raw)
            data = as_dict(response)
            # A photo may appear in both the library and the selected template.
            referenced = {row['assetId'] for row in data['photoInfos'] if row.get('assetId')}
            referenced.update(slot['assetId'] for template in data['templateInfos']
                              for slot in template['slotInfos'] if slot.get('assetId'))
            entry.update(status='exported', photos=len(response.photoInfos), media_keys=[])
            for asset_id in sorted(referenced):
                key = media_key('event', asset_id)
                entry['media_keys'].append(key)
                if key not in assets:
                    asset = {'asset_id': asset_id, 'status': 'downloaded'}
                    assets[key] = asset
                    for variant in ('original', 'thumbnail'):
                        try:
                            asset[variant] = photos.firebase_image(asset_id, variant,
                                archive.output / 'photos' / 'event-media' / key, bucket='event-idolypride')
                        except Exception as error:
                            asset.update(status='failed')
                            asset[variant] = {'status': 'failed', 'reason': safe_failure(error)}
                if assets[key]['status'] == 'failed':
                    entry['status'] = 'failed'
        except Exception as error:
            entry.update(status='failed', reason=safe_failure(error))
    coverage.update(status='failed' if any(e['status'] == 'failed' for e in coverage['albums']) else 'complete_with_gaps',
                    reason='Only the current Shelf can be enumerated; older or locked albums may be inaccessible.')
    write_json(archive.output / 'photos' / 'event-albums.json', {**coverage, 'assets': assets})
    archive.save()


def parse_auth_xml(contents):
    try:
        root = ElementTree.fromstring(contents)
        records = [json.loads(node.text) for node in root
                   if 'GET_TOKEN_RESPONSE' in node.attrib.get('name', '')]
    except (ValueError, ElementTree.ParseError, TypeError):
        raise ExportError('Invalid Firebase authentication XML.') from None
    if len(records) != 1 or not isinstance(records[0], dict):
        raise ExportError('Expected one Firebase token record; select the intended account explicitly.')
    return records[0]


def adb_output(adb, serial, *args):
    try:
        result = subprocess.run([adb, '-s', serial, *args], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ExportError('ADB unavailable or timed out.') from None
    if result.returncode:
        raise ExportError('ADB command failed; check connection and game run-as/root access.')
    return result.stdout


def device_auth(adb, serial, package):
    if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
        raise ExportError('Invalid Android package name.')
    # run-as works with the existing debuggable patched client, without changing it.
    try:
        listing = adb_output(adb, serial, 'shell', 'run-as', package, 'ls', 'shared_prefs').decode()
        prefix = ('exec-out', 'run-as', package, 'cat')
        parent = 'shared_prefs/'
    except ExportError:
        uid = adb_output(adb, serial, 'shell', 'id', '-u').strip()
        if uid != b'0':
            raise ExportError('Game data is inaccessible: use a debuggable client, existing root access, or an auth XML file.') from None
        parent = '/data/user/0/' + package + '/shared_prefs/'
        listing = adb_output(adb, serial, 'shell', 'ls', parent).decode()
        prefix = ('exec-out', 'cat')
    names = [name for name in listing.splitlines()
             if re.fullmatch(r'com\.google\.firebase\.auth\.api\.Store\.[A-Za-z0-9_+\-=]+\.xml', name)]
    if len(names) != 1:
        raise ExportError('Expected one Firebase auth store in this app; provide an explicit auth XML instead.')
    record = parse_auth_xml(adb_output(adb, serial, *prefix, parent + names[0]))
    package_info = adb_output(adb, serial, 'shell', 'dumpsys', 'package', package).decode()
    versions = re.findall(r'\bversionName=([^\s]+)', package_info)
    if not versions:
        raise ExportError('Could not determine installed game version.')
    return record, versions[0]


def device_firebase_api_key(adb, serial, package):
    """Read the installed app's resource using Android's built-in query command."""
    if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
        raise ExportError('Invalid Android package name.')
    try:
        contents = adb_output(adb, serial, 'shell', 'cmd', 'overlay', 'lookup',
                              '--user', '0', package, package + ':string/google_api_key')
        key = contents.decode('utf-8').strip()
    except (ExportError, UnicodeDecodeError):
        raise ExportError('Cannot read Firebase API key from the game; provide --firebase-settings or IDOLY_FIREBASE_API_KEY.') from None
    if not re.fullmatch(r'AIza[A-Za-z0-9_-]{35}', key):
        raise ExportError('Game resource did not return a Firebase API key; provide --firebase-settings or IDOLY_FIREBASE_API_KEY.')
    return key


def resolve_firebase_api_key(args):
    """Explicit configuration overrides the environment and device discovery."""
    if args.firebase_settings:
        settings = read_json(args.firebase_settings)
        key = settings.get('api_key') if isinstance(settings, dict) else None
    else:
        key = os.environ.get('IDOLY_FIREBASE_API_KEY', '')
        if not key and args.device:
            return device_firebase_api_key(args.adb, args.device, args.package)
        if not key:
            return ''  # An unexpired access token can still be used without a refresh.
    if not isinstance(key, str) or not key.strip():
        raise ExportError('Firebase API key configuration must contain a nonempty api_key string.')
    return key.strip()


class Client:
    """Small explicit RPC allowlist. Never runs SolisClient login scenarios."""
    ALLOWED = {'System.Check', 'Auth.Login', 'Master.Get', 'User.Get', 'Gift.List', 'Gift.ListHistory',
               'Gacha.History', 'Photo.GetImageURL', 'Showcase.Top', 'Shelf.Top', 'Shelf.AlbumTop',
               'Profile.FindUser', *CATALOG_QUERIES}

    def __init__(self, directory, api, app_version):
        import grpc
        import requests
        self.grpc, self.requests, self.api = grpc, requests, api
        self.config = read_json(Path(directory) / 'client_config.json')
        self.marshaller = importlib.import_module('marshaller')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', app_version):
            raise ExportError('Invalid app version.')
        self.metadata = dict(self.config['grpc'])
        self.metadata.pop('grpc-timeout', None)
        self.metadata.pop('x-auth-token', None)
        self.metadata['x-app-version'] = app_version
        self.app_version = app_version
        roots = (Path(directory) / 'iprroots.pem').read_bytes()
        self.channel = grpc.secure_channel(API_HOST, grpc.ssl_channel_credentials(root_certificates=roots),
            options=[('grpc.primary_user_agent', 'grpc-csharp/2.37.0-dev grpc-c/15.0.0 (android; chttp2)'),
                     ('grpc.max_receive_message_length', 128 * 1024 * 1024)])

    def close(self):
        self.channel.close()

    def call(self, route, request, metadata=None):
        from google.protobuf.message_factory import GetMessageClass
        if route not in self.ALLOWED:
            raise ExportError('RPC is not in the export allowlist.')
        service, method = route.split('.')
        definition = self.api.DESCRIPTOR.services_by_name[service].methods_by_name[method]
        response_class = GetMessageClass(definition.output_type)
        rpc = self.channel.unary_unary('/api.' + service + '/' + method,
            request_serializer=lambda value: self.marshaller.serialize(value.SerializeToString()),
            response_deserializer=self.marshaller.deserialize)
        try:
            raw, call = rpc.with_call(request, metadata=list((self.metadata if metadata is None else metadata).items()), timeout=45)
        except self.grpc.RpcError as error:
            codes = [str(v) for k, v in [*(error.initial_metadata() or []), *(error.trailing_metadata() or [])]
                     if k == 'x-error-code' and re.fullmatch(r'\d{1,6}', str(v))]
            code = error.code().name
            if '1003' in codes:
                raise ExportError('DateChanged (1003): open the game and finish normal login, then retry; no Home.Login was sent.') from None
            if '1005' in codes:
                raise ExportError('RPC ' + route + ' unavailable: InFunctionMaintenance (1005).', application_codes=codes) from None
            raise ExportError('RPC ' + route + ' failed: ' + code + '; application codes=' + ','.join(codes),
                              application_codes=codes) from None
        codes = [str(v) for k, v in [*(call.initial_metadata() or []), *(call.trailing_metadata() or [])]
                 if k == 'x-error-code' and re.fullmatch(r'\d{1,6}', str(v)) and str(v) != '0']
        if codes:
            raise ExportError('RPC ' + route + ' returned application codes=' + ','.join(codes), application_codes=codes)
        return response_class.FromString(raw), raw

    def token(self, record, api_key, force=False):
        expires = float(record.get('issued_at', 0)) / 1000 + float(record.get('expires_in', 0))
        if not force and record.get('access_token') and expires > time.time() + 90:
            return record['access_token']
        renewable = record.get('refresh_token') or record.get('refreshToken') or record.get('firebase_refresh_token')
        if not renewable or not api_key:
            raise ExportError('A Firebase refresh token and API key are required to refresh authentication.')
        try:
            response = self.requests.post('https://securetoken.googleapis.com/v1/token',
                params={'key': api_key}, headers={**self.config['firebase'], 'Content-Type': 'application/x-www-form-urlencoded'},
                data={'grant_type': 'refresh_token', 'refresh_token': renewable}, timeout=30, allow_redirects=False)
        except self.requests.RequestException:
            raise ExportError('Firebase refresh transport failed; no automatic retry.') from None
        if response.status_code != 200:
            raise ExportError(f'Firebase refresh rejected (HTTP {response.status_code}).')
        data = response.json()
        if not data.get('id_token'):
            raise ExportError('Firebase refresh response has no ID token.')
        record.update(access_token=data['id_token'], refresh_token=data.get('refresh_token', renewable),
                      issued_at=int(time.time() * 1000), expires_in=data.get('expires_in', 3600))
        return record['access_token']

    def authenticate(self, record, api_key):
        from google.protobuf.empty_pb2 import Empty
        self.call('System.Check', self.api.SystemCheckRequest(),
                  {'x-app-version': self.app_version, 'content-type': self.metadata['content-type']})
        headers = {**self.config['qseed'], 'X-AppVersion': self.app_version}
        try:
            response = self.requests.get('https://id.qseed.jp/getId', headers=headers, timeout=30, allow_redirects=False)
        except self.requests.RequestException:
            raise ExportError('QSeed request failed.') from None
        if response.status_code != 200 or not response.text or len(response.text) > 4096:
            raise ExportError('QSeed request rejected.')
        self.metadata['x-seed-id'] = response.text.strip()
        token = self.token(record, api_key)
        self.call('System.Check', self.api.SystemCheckRequest(firebaseIDToken=token))
        login, _ = self.call('Auth.Login', self.api.AuthLoginRequest(firebaseIDToken=token))
        if login.requiredFirebaseReauthenticate:
            token = self.token(record, api_key, force=True)
            login, _ = self.call('Auth.Login', self.api.AuthLoginRequest(firebaseIDToken=token))
        if login.requiredFirebaseReauthenticate or not login.gameAuthToken:
            raise ExportError('Account requires additional authentication; no create/migrate fallback was attempted.')
        self.metadata['x-auth-token'] = login.gameAuthToken
        master, raw = self.call('Master.Get', Empty())
        if not master.masterTag.version:
            raise ExportError('Master.Get returned no master version.')
        self.metadata['x-master-version'] = master.masterTag.version
        return master, raw


def fetch(args, api):
    from google.protobuf.empty_pb2 import Empty
    version = args.app_version
    if args.device:
        record, installed = device_auth(args.adb, args.device, args.package)
        version = version or installed
    elif args.auth_xml:
        record = parse_auth_xml(Path(args.auth_xml).read_bytes())
    else:
        record = read_json(args.token_file)
    if not version:
        raise ExportError('--app-version is required without --device.')
    key = resolve_firebase_api_key(args)
    output = args.output or EXPORTS_ROOT / datetime.now().strftime('%Y%m%d-%H%M%S')
    archive = Archive(output, api, 'live_api')
    archive.manifest['requested_sections'] = (['user', 'master', 'profile_display']
        + ([] if args.skip_master_data else ['master_data'])
        + ([] if args.skip_gifts else ['gifts', 'gift_history'])
        + ([] if args.skip_catalogs else list(CATALOG_QUERIES))
        + (['gacha_history'] if args.gacha_history else [])
        + (['photos', 'event_albums'] if args.photos else [])
        + (['showcases'] if args.showcases else []))
    archive.manifest['coverage'].update({key: {'status': 'pending'} for key in archive.manifest['requested_sections']})
    for section, enabled in (('photos', args.photos), ('event_albums', args.photos), ('showcases', args.showcases)):
        if not enabled:
            archive.manifest['coverage'][section] = {'status': 'not_requested'}
    archive.save()
    client = downloader = None
    try:
        client = Client(args.solis_dir, api, version)
        master, master_raw = client.authenticate(record, key)
        downloader = Downloader()
        archive.manifest['app_version'] = version
        archive.manifest['master_version'] = client.metadata['x-master-version']
        message, raw = client.call('User.Get', Empty())
        archive.account(message, raw)
        account = as_dict(message)
        export_master(master, master_raw, archive, downloader, args.skip_master_data)
        try:
            # Only query this account's rendered profile fields, not other players.
            # The request takes the public ID; the response identifies the server ID.
            profile, raw = client.call('Profile.FindUser', api.ProfileFindUserRequest(userId=message.userPublic.publicUserId))
            if profile.userId != message.userPublic.serverUserId:
                raise ExportError('Profile response does not match the exported account.')
            archive.response('profile-display', profile, raw)
            archive.manifest['coverage']['profile_display'] = {'status': 'server_returned_response'}
        except Exception as error:
            archive.manifest['coverage']['profile_display'] = {'status': 'failed', 'reason': safe_failure(error)}
        if not args.skip_gifts:
            export_gifts(lambda offset: client.call('Gift.List', api.GiftListRequest(offset=offset, sortType=1, desc=False)),
                         archive, args.max_gift_pages)
            message, raw = client.call('Gift.ListHistory', Empty())
            archive.response('gift-history', message, raw)
            write_json(archive.output / 'tables' / 'giftHistory.json', as_dict(message).get('gifts', []))
            archive.manifest['coverage']['gift_history'] = {'status': 'server_returned_window_only', 'records': len(message.gifts)}
        else:
            archive.manifest['coverage']['gifts'] = {'status': 'not_requested'}
        if not args.skip_catalogs:
            for route in CATALOG_QUERIES:
                name = route.replace('.', '-').lower()
                try:
                    response, raw = client.call(route, Empty())
                    archive.response(name, response, raw)
                    archive.manifest['coverage'][route] = {'status': 'server_returned_response'}
                except ExportError as error:
                    archive.manifest['coverage'][route] = {'status': 'failed', 'reason': str(error)}
                archive.save()
        else:
            archive.manifest['coverage']['catalogs'] = {'status': 'not_requested'}
        if args.gacha_history:
            archive.manifest['coverage']['gacha_history'] = {'status': 'server_returned_windows_only'}
            enum = api.GachaHistoryRequest.DESCRIPTOR.fields_by_name['gachaType'].enum_type
            for value in enum.values:
                if value.number == 0:
                    continue
                scope = f'gacha-history-{value.number}'
                try:
                    response, raw = client.call('Gacha.History', api.GachaHistoryRequest(gachaType=value.number))
                    archive.response(scope, response, raw)
                    archive.manifest['coverage'][scope] = {'status': 'server_returned_window_only',
                        'gacha_type': value.name, 'records': len(response.histories)}
                except ExportError as error:
                    archive.manifest['coverage'][scope] = {'status': 'failed', 'reason': str(error)}
                archive.save()
        photos = PhotoExporter(args, archive, client, downloader, record, key, account)
        for section, enabled, operation in (
                ('photos', args.photos, lambda: photos.export(account)),
                ('event_albums', args.photos, lambda: export_albums(client, archive, photos)),
                ('showcases', args.showcases, lambda: export_showcases(client, archive, account, photos))):
            if enabled:
                try:
                    operation()
                except Exception as error:
                    archive.manifest['coverage'][section] = {'status': 'failed', 'reason': safe_failure(error)}
                archive.save()
        failed = [key for key, value in archive.manifest['coverage'].items() if value['status'] in ('failed', 'pending')]
        gaps = any(value['status'] == 'complete_with_gaps' for value in archive.manifest['coverage'].values())
        archive.finish('incomplete' if failed else 'complete_with_gaps' if gaps else 'complete_for_requested_scope')
        if failed:
            raise ExportError('Some requested queries failed; inspect manifest coverage: ' + ', '.join(failed))
    except Exception as error:
        archive.manifest['failure'] = str(error) if isinstance(error, ExportError) else type(error).__name__
        for section in archive.manifest['coverage'].values():
            if section['status'] == 'pending':
                section['status'] = 'not_completed'
        archive.finish('incomplete')
        raise
    finally:
        if downloader:
            downloader.close()
        if client:
            client.close()
    print(json.dumps({'output': str(archive.output), 'status': archive.manifest['status'],
                      'fields': archive.manifest['coverage']['user']['field_count'],
                      'unknown_fields': sum(len(e['unknown_fields']) for e in archive.manifest['responses'])}, ensure_ascii=False))


def latest_export():
    """Select the latest default export, including incomplete attempts."""
    candidates = [path for path in EXPORTS_ROOT.iterdir()
                  if re.fullmatch(r'\d{8}-\d{6}', path.name)
                  and path.is_dir() and not path.is_symlink()] if EXPORTS_ROOT.is_dir() else []
    if not candidates:
        raise ExportError('No timestamped export found in exports/; run fetch first or specify a directory to verify.')
    return max(candidates, key=lambda path: path.name)


def verify_archive(directory):
    directory = Path(directory).resolve()
    manifest = read_json(directory / 'manifest.json')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ExportError('Manifest has no file hashes; archive is unfinished or from an older format.')
    for name, expected in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory) or path.is_symlink():
            raise ExportError('Manifest contains an unsafe file path.')
        if not path.is_file() or file_hash(path) != expected:
            raise ExportError('Archive integrity check failed; a file is missing or modified.')
    return {'files_verified': len(files), 'status': manifest['status'],
            'note': 'Hashes verify files against this manifest, not authenticity or server completeness.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solis-dir', type=Path, help='Trusted SolisClient protocol/transport directory')
    parser.add_argument('--descriptor-set', type=Path, help='Archive schema.pb for standalone offline parsing')
    sub = parser.add_subparsers(dest='command', required=True)
    live = sub.add_parser('fetch', help='Read current account from official API')
    source = live.add_mutually_exclusive_group(required=True)
    source.add_argument('--device', help='ADB serial, e.g. emulator-5560')
    source.add_argument('--auth-xml', type=Path)
    source.add_argument('--token-file', type=Path, help='Private JSON containing refresh_token; never put tokens on command line')
    live.add_argument('--adb', default='adb')
    live.add_argument('--package', default=PACKAGE)
    live.add_argument('--firebase-settings', type=Path, help='Optional JSON containing api_key; overrides IDOLY_FIREBASE_API_KEY and automatic --device lookup')
    live.add_argument('--app-version')
    live.add_argument('--output', type=Path, help='Default: exports/<timestamp>/ in the tool directory')
    live.add_argument('--skip-gifts', action='store_true')
    live.add_argument('--skip-catalogs', action='store_true', help='Skip quest/story catalogs and diary read states')
    live.add_argument('--skip-master-data', action='store_true', help='Save Master.Get metadata but skip encrypted master packages')
    live.add_argument('--photos', action='store_true', help='Download photo originals/thumbnails and query available event albums')
    live.add_argument('--showcases', action='store_true', help='Export current showcase layouts and preview images')
    catalog = live.add_mutually_exclusive_group()
    catalog.add_argument('--octo-cache', type=Path, help='Optional octocacheevai file for fixed photos; otherwise discovered via --device')
    catalog.add_argument('--octo-manifest', type=Path, help='Optional decoded full Octo catalog JSON for fixed photos')
    live.add_argument('--unity-version', default='2022.3.57f1', help='Fallback Unity version for bundles with stripped version headers')
    live.add_argument('--gacha-history', action='store_true', help='Also export server-retained history for each known gacha type')
    live.add_argument('--max-gift-pages', type=int, default=1000)
    offline = sub.add_parser('parse', help='Decode a decrypted User.Get protobuf snapshot offline')
    offline.add_argument('input', type=Path)
    offline.add_argument('--output', required=True, type=Path)
    schema = sub.add_parser('schema', help='Inspect User.Get fields without credentials or network')
    schema.add_argument('--output', required=True, type=Path)
    verify = sub.add_parser('verify', help='Check saved archive file hashes; no network or protocol modules')
    verify.add_argument('directory', nargs='?', type=Path, help='Default: latest timestamped export in exports/')
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            directory = args.directory if args.directory is not None else latest_export()
            print(json.dumps({'output': str(directory.resolve()), **verify_archive(directory)}, ensure_ascii=False))
            return 0
        if args.command == 'fetch' and (not args.solis_dir or args.descriptor_set):
            raise ExportError('Live export requires --solis-dir and does not accept --descriptor-set.')
        api = load_protocol(args.solis_dir, args.descriptor_set)
        if args.command == 'fetch':
            if args.max_gift_pages < 1:
                raise ExportError('max-gift-pages must be positive.')
            fetch(args, api)
        elif args.command == 'parse':
            raw = args.input.read_bytes()
            message = api.UserGetResponse.FromString(raw)
            archive = Archive(args.output, api, 'offline_protobuf')
            try:
                archive.account(message, raw)
                archive.manifest['coverage']['gifts'] = {'status': 'not_in_input'}
                archive.finish('decoded_input_only')
            except Exception as error:
                archive.manifest['coverage']['user'] = {'status': 'failed'}
                archive.manifest['failure'] = str(error) if isinstance(error, ExportError) else type(error).__name__
                archive.finish('incomplete')
                raise
            print(str(archive.output))
        else:
            write_json(args.output, field_schema(api.UserGetResponse.DESCRIPTOR))
        return 0
    except ExportError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted; any partial export remains marked in_progress/incomplete.', file=sys.stderr)
        return 130
    except Exception as error:
        # Exception text can contain request headers, signed URLs, token bodies or XML.
        print('Export failed (' + type(error).__name__ + '); partial files are retained. No credentials logged.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
