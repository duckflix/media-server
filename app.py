#!/usr/bin/env python3
import aiofiles
import argparse
import asyncio
import io
import json
import logging
import re
import starlette.responses
import subprocess
import sys
import typing
import uvicorn
import xml.etree.ElementTree

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from http import HTTPStatus
from pathlib import Path
from starlette.background import BackgroundTask
from starlette.concurrency import run_until_first_complete
from starlette.responses import Response
from starlette.types import Receive, Scope, Send


class MKVTags(object):
    @classmethod
    def fromstring(cls, s):
        root = xml.etree.ElementTree.fromstring(s)
        return cls(root)

    def __init__(self, root):
        self.root = root

    def tmdb_id(self):
        for tmdb_tag in self.root.findall(".//Tag/Targets/TargetTypeValue[.='50']/../../Simple/Name[.='TMDB']/../String"):
            return tmdb_tag.text


class Library(object):
    def __init__(self):
        self.movies = {}

    def scan(self, base_dir):
        movies = {}

        for mkv_path in Path(base_dir).rglob('*.mkv'):
            # Extract the MKV tags xml
            extract_result = subprocess.run(['mkvextract', mkv_path.as_posix(), 'tags', '/dev/stdout'], capture_output=True)
            if extract_result.returncode != 0:
                continue

            if extract_result.stdout == b'':
                continue

            # Read the TMDB ID MKV tag
            try:
                tags = MKVTags.fromstring(extract_result.stdout)
            except Exception:
                LOG.exception('failed to parse tag xml from %s: %s', mkv_path.as_posix(), extract_result.stdout)
                continue
            tmdb_id = tags.tmdb_id()
            if not tmdb_id:
                continue

            LOG.info('%s = %s', mkv_path.as_posix(), tmdb_id)

            # Extract the tmdb.json for this mkv
            identify_result = subprocess.run(['mkvmerge', '--identify', mkv_path.as_posix()], capture_output=True)
            if identify_result.returncode == 0:
                # Find the attachment by name
                for line in identify_result.stdout.decode('utf-8').split('\n'):
                    # Sort out which attachment is tmdb.json
                    m = re.match(r"Attachment ID (?P<attachment_id>[0-9]+): type '(?P<mime_type>[^']+)',(.*,?) file name '(?P<filename>[^']+)'.*", line)
                    if m and m.group('filename') == 'tmdb.json':
                        attachment_id = int(m.group('attachment_id'))
                    else:
                        continue

                    # Extract the tmdb.json attachment
                    tmdb_extract_result = subprocess.run(['mkvextract', '--quiet', mkv_path.as_posix(), 'attachments', f'{attachment_id}:/dev/stdout'], capture_output=True)
                    if tmdb_extract_result.returncode == 0:
                        # Parse the tmdb.json
                        tmdb_details = json.load(io.BytesIO(tmdb_extract_result.stdout))

                        # Stop looking at attachments since the tmdb.json was found and extracted
                        break
                else:
                    LOG.warning('no tmdb.json attachment found for %s, ignoring mkv', mkv_path.as_posix())
                    continue

            # Extract the cover.jpg for this mkv
            identify_result = subprocess.run(['mkvmerge', '--identify', mkv_path.as_posix()], capture_output=True)
            if identify_result.returncode == 0:
                # Find the attachment by name
                for line in identify_result.stdout.decode('utf-8').split('\n'):
                    # Sort out which attachment is tmdb.json
                    m = re.match(r"Attachment ID (?P<attachment_id>[0-9]+): type '(?P<mime_type>[^']+)',(.*,?) file name '(?P<filename>[^']+)'.*", line)
                    if m and m.group('filename') == 'cover.jpg':
                        attachment_id = int(m.group('attachment_id'))
                    else:
                        continue

                    # Extract the tmdb.json attachment
                    cover_extract_result = subprocess.run(['mkvextract', '--quiet', mkv_path.as_posix(), 'attachments', f'{attachment_id}:/dev/stdout'], capture_output=True)
                    if cover_extract_result.returncode == 0:
                        # Parse the tmdb.json
                        cover_jpeg_data = cover_extract_result.stdout

                        # Stop looking at attachments since the cover.jpg was found and extracted
                        break
                else:
                    LOG.warning('no cover.jpg attachment found for %s, ignoring mkv', mkv_path.as_posix())
                    continue

            movies[tmdb_id] = {'path': mkv_path, 'tmdb': tmdb_id, 'tmdb_details': tmdb_details, 'cover_jpeg_data': cover_jpeg_data}

        self.movies = movies


# Open a logger
LOG = logging.getLogger(__name__)

class DuckFlixAPI(FastAPI):
    def __init__(self):
        super().__init__()
        self.media = Library()


# Create a new ASWG app with FastAPI
app = DuckFlixAPI()

# Explictly allow any CORS origin (hence using the allow_origin_regex as it sends explicit origin allows)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex='.*',
    allow_headers=['Range'],
)


@app.get('/movie/genres.json')
async def movie_genre_list():
    return [
        {
          "id": 28,
          "name": "Action"
        },
        {
          "id": 12,
          "name": "Adventure"
        },
        {
          "id": 16,
          "name": "Animation"
        },
        {
          "id": 35,
          "name": "Comedy"
        },
        {
          "id": 80,
          "name": "Crime"
        },
        {
          "id": 99,
          "name": "Documentary"
        },
        {
          "id": 18,
          "name": "Drama"
        },
        {
          "id": 10751,
          "name": "Family"
        },
        {
          "id": 14,
          "name": "Fantasy"
        },
        {
          "id": 36,
          "name": "History"
        },
        {
          "id": 27,
          "name": "Horror"
        },
        {
          "id": 10402,
          "name": "Music"
        },
        {
          "id": 9648,
          "name": "Mystery"
        },
        {
          "id": 10749,
          "name": "Romance"
        },
        {
          "id": 878,
          "name": "Science Fiction"
        },
        {
          "id": 10770,
          "name": "TV Movie"
        },
        {
          "id": 53,
          "name": "Thriller"
        },
        {
          "id": 10752,
          "name": "War"
        },
        {
          "id": 37,
          "name": "Western"
        }
    ]


@app.get('/movies.json')
async def movies_list():
    return [movie['tmdb'] for movie in app.media.movies.values()]


@app.get('/movie/{movie_id}/attachment/cover.jpg')
async def movie_cover(movie_id: int):
    tmdb_id = 'movie/' + str(movie_id)

    try:
        movie = app.media.movies[tmdb_id]
    except KeyError:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail=f'{tmdb_id} not found')

    try:
        cover_file = io.BytesIO(movie['cover_jpeg_data'])
    except KeyError:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail=f'cover.jpg for {tmdb_id} not found')

    return StreamingResponse(cover_file, media_type='image/jpeg')


@app.get('/movie/{movie_id}/attachment/tmdb.json')
async def movie_tmdb_details(movie_id: int):
    tmdb_id = 'movie/' + str(movie_id)

    try:
        movie = app.media.movies[tmdb_id]
    except KeyError:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail=f'{tmdb_id} not found')

    try:
        cover_file = io.BytesIO(json.dumps(movie['tmdb_details']).encode('utf-8'))
    except KeyError:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail=f'tmdb json for {tmdb_id} not found')

    return StreamingResponse(cover_file, media_type='application/json')


@app.get('/movie/{movie_id}/download')
async def stream_movie(movie_id: int, request: Request):
    tmdb_id = 'movie/' + str(movie_id)

    try:
        movie = app.media.movies[tmdb_id]
    except KeyError:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail=f'{tmdb_id} not found')

    request_range = request.headers.get('Range')
    if request_range:
        return PartialFileResponse(movie['path'].as_posix(), request_range, media_type='video/matroska')

    return FileResponse(
        movie['path'].as_posix(),
        headers={'Accept-Range': 'bytes'},
        status_code=int(HTTPStatus.OK)
    )


class PartialFileResponse(Response):
    chunk_size = 4096

    def __init__(
        self,
        path: typing.Union[str, "os.PathLike[str]"],
        request_range: str,
        headers: dict = None,
        media_type: str = None,
        method: str = None,
        background: BackgroundTask = None,
    ) -> None:
        super().__init__(headers=headers, background=background)

        self.path = path
        self.send_header_only = method is not None and method.upper() == "HEAD"
        self.request_range = request_range
        self.media_type = media_type

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with aiofiles.open(self.path, mode='rb') as f:
                # Get the total file size and reseek to the beginning of the file
                SEEK_FROM_END = 2
                SEEK_FROM_BEG = 0
                await f.seek(0, SEEK_FROM_END)
                total_size = await f.tell()
                await f.seek(0, SEEK_FROM_BEG)

                # Parse range units per https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Range#syntax
                range_unit = 'bytes'
                request_range = self.request_range
                if '=' in request_range:
                    range_unit, request_range = request_range.split('=')

                # Only byte ranges are supported, other units cannot be processed
                if range_unit != 'bytes':
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
                        'headers': self.raw_headers
                        })
                    await send({
                        'type': 'http.response.body',
                        'body': json.dumps({'detail': 'non-bytes ranges are not supported'}).encode('utf-8'),
                        'more_body': False,
                        })
                    return

                # Do not support multiple byte range requests
                if ',' in request_range:
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
                        'headers': self.raw_headers
                        })
                    await send({
                        'type': 'http.response.body',
                        'body': json.dumps({'detail': 'multi-range not supported'}).encode('utf-8'),
                        'more_body': False,
                        })
                    return

                # Process range start/end per https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Range#directives
                range_start, range_end = request_range.strip().split('-')

                # Try to convert range start/end to byte counts
                try:
                    if range_start:
                        range_start = int(range_start)
                    if range_end:
                        range_end = int(range_end)
                except Exception:
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
                        'headers': self.raw_headers
                        })
                    await send({
                        'type': 'http.response.body',
                        'body': json.dumps({'detail': 'invalid int in range'}).encode('utf-8'),
                        'more_body': False,
                        })
                    return

                # Require range start positions
                if range_start == '':
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
                        'headers': self.raw_headers
                        })
                    await send({
                        'type': 'http.response.body',
                        'body': json.dumps({'detail': 'range start required'}).encode('utf-8'),
                        'more_body': False,
                        })
                    return

                # Do not support range end
                if range_end != '':
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
                        'headers': self.raw_headers
                        })
                    await send({
                        'type': 'http.response.body',
                        'body': json.dumps({'detail': 'range end not supported'}).encode('utf-8'),
                        'more_body': False,
                        })
                    return

                # Seek to the beginning of the range in the content
                await f.seek(range_start, SEEK_FROM_BEG)

                # Set the appropriate headers for ranged content
                range_headers = dict(self.raw_headers)
                range_headers.update({
                    'Accept-Ranges': 'bytes',
                    'Content-Range': f'bytes {range_start}-{total_size-1}/{total_size}',
                    'Content-Length': str(total_size-range_start),
                    'Content-Type': self.media_type,
                })
                self.init_headers(range_headers)

                async def listen_for_disconnect(receive):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return

                async def stream_response(f, send):
                    await send({
                        'type': 'http.response.start',
                        'status': int(HTTPStatus.PARTIAL_CONTENT),
                        'headers': self.raw_headers,
                        })

                    more_body = True
                    while more_body:
                        chunk = await f.read(self.chunk_size)
                        more_body = len(chunk) == self.chunk_size
                        await send({
                            'type': 'http.response.body',
                            'body': chunk,
                            'more_body': more_body,
                            })

                await run_until_first_complete(
                    (listen_for_disconnect, {'receive': receive}),
                    (stream_response, {'send': send, 'f': f})
                )
        finally:
            if self.background:
                await self.background()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('media_path', help='Directory containing mkv files')
    return p.parse_args()


def main():
    args = parse_args()
    print(f'Scanning {args.media_path}')
    app.media.scan(args.media_path)
    uvicorn.run(app, host='0.0.0.0', port=8000, log_level='info')


if __name__ == '__main__':
    sys.exit(main())
