#!/usr/bin/env python3
import argparse
import http.server
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree

from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse


LOG = logging.getLogger('')


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--listen-address', default='0.0.0.0')
    p.add_argument('--listen-port', default=8080)
    p.add_argument('--allow-ip', default=[], action='append')
    p.add_argument('base_dir')
    return p.parse_args()


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
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.movies = {}

    def scan(self):
        movies = {}

        for mkv_path in Path(self.base_dir).rglob('*.mkv'):
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

            # Extract the tmdb.json for this attachment
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

            movies[tmdb_id] = {'path': mkv_path, 'tmdb': tmdb_id, 'tmdb_details': tmdb_details}

        self.movies = movies


class HandlerFactory(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def request_handler(self, *args, **kwargs):
        full_kwargs = dict(self.kwargs)
        full_kwargs.update(kwargs)
        return Handler(*args, **full_kwargs)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'duckflix-media-server/0.1'

    def __init__(self, *args, library=None, allowed_clients=[], **kwargs):
        self.allowed_clients = allowed_clients
        self.library = library
        return http.server.BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

    def do_GET(self):
        self.method = 'GET'
        return self.serve()

    def do_HEAD(self):
        self.method = 'HEAD'
        return self.serve()

    # TODO: Overwrite send_error to also send CORS headers

    def serve(self):
        # Parse the path and querystring components
        self.parsed_path = urlparse(self.path, scheme='http')
        self.parsed_qs = parse_qs(self.parsed_path.query)

        client_ip = self.client_address[0]
        if client_ip not in self.allowed_clients:
            self.log_error('client %s not allowed', client_ip)
            return self.send_error(HTTPStatus.FORBIDDEN)

        if self.parsed_path.path == '/movie/genres.json':
            return self.send_movie_genres_index()

        if self.parsed_path.path == '/movies.json':
            return self.send_movies_index()

        m = re.match(r'/(?P<movie_id>movie/[0-9]+)/download', self.parsed_path.path)
        if m:
            return self.send_movie_mkv(m.group('movie_id'))

        m = re.match(r'/(?P<movie_id>movie/[0-9]+)/attachment/(?P<attachment_name>[^/]+)', self.parsed_path.path)
        if m:
            return self.send_movie_attachment(m.group('movie_id'), m.group('attachment_name'))

        # Send the response
        return self.send_error(HTTPStatus.FORBIDDEN)

    def send_fileobj(self, f, mime_type=None):
        # Seek to the end of the file and get its length
        SEEK_FROM_END = 2
        SEEK_FROM_BEG = 0
        f.seek(0, SEEK_FROM_END)
        total_size = f.tell()

        # Determine the requsted range of content
        request_range = self.headers.get('Range')
        if request_range:
            self.log_message('request range: %s', request_range)

            # Parse range units per https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Range#syntax
            range_unit = 'bytes'
            if '=' in request_range:
                range_unit, request_range = request_range.split('=')

            # Only byte ranges are supported, other units cannot be processed
            if range_unit != 'bytes':
                self.log_message('unsupported range: non-bytes unit was specified')
                return self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            # Do not support multiple byte range requests
            if ',' in request_range:
                self.log_message('unsupported range: multiple ranges were specified')
                return self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            # Process range start/end per https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Range#directives
            range_start, range_end = request_range.strip().split('-')

            # Try to convert range start/end to byte counts
            try:
                self.log_message('range_start: %s', range_start)
                if range_start:
                    range_start = int(range_start)
                self.log_message('range_end: %s', range_end)
                if range_end:
                    range_end = int(range_end)
            except Exception:
                self.log_message('unsupported range: failed to convert ranges to integers')
                return self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            # Require range start positions
            if range_start == '':
                self.log_message('unsupported range: missing range-start')
                return self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            # Do not support range end
            if range_end != '':
                self.log_message('unsupported range: range-end was specified')
                return self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            # Seek to the beginning of the range in the content
            f.seek(range_start, SEEK_FROM_BEG)

            # Set the approtriate code/headers for ranged content
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header('Content-Range', f'bytes {range_start}-{total_size-1}/{total_size}')
            self.send_header('Content-Length', str(total_size-range_start))
        else:
            # Seek to the beginning of the content since no range was specifieg
            f.seek(0, SEEK_FROM_BEG)

            # Set the appropriate status code/headers for non-ranged content
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Length', total_size)

        # If a mimetype is known send it to the client
        if mime_type:
            self.send_header('Content-Type', mime_type)

        # This header enables http clients to request parts of content, which is important for seeking streaming video
        self.send_header('Accept-Ranges', 'bytes')

        # This header enables async requests from webpages in browsers enforcing CORS
        origin = self.headers.get('Origin')
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)

        # No more headers need to be sent
        self.end_headers()

        if self.method == 'GET':
            try:
                shutil.copyfileobj(f, self.wfile)
            except ConnectionResetError:
                self.log_error('connection reset by peer %s:%s', self.client_address[0], self.client_address[1])

    def send_movie_genres_index(self):
	# From https://api.themoviedb.org/3/genre/movie/list?api_key=<<api_key>>&language=en-US
        genres = [
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
        f = io.BytesIO(json.dumps(genres).encode('utf-8'))
        self.send_fileobj(f, mime_type='application/json')

    def send_movies_index(self):
        # Look through the movie library to build an index of the known movies
        movies = []
        for library_entry in self.library.movies.values():
            # If one or more genre_id values were provided in the url then only include movies with that genre_id
            if 'genre_id' in self.parsed_qs:
                allowed_genre_ids = [int(genre_id) for genre_id in self.parsed_qs['genre_id']]
                movie_genre_ids = library_entry['tmdb_details']['genre_ids']
                for genre_id in movie_genre_ids:
                    if genre_id in allowed_genre_ids:
                        break
                else:
                    continue

            # If one or more original_language values were provided in the url then only include movies with that original_language
            if 'original_language' in self.parsed_qs:
                allowed_original_languages = self.parsed_qs['original_language']
                if library_entry['tmdb_details']['original_language'] not in allowed_original_languages:
                    continue

            # If all specified filters matched then include this movie in the index
            movies.append(library_entry['tmdb_details'])

        # Send the movie index json to the client
        f = io.BytesIO(json.dumps(movies).encode('utf-8'))
        self.send_fileobj(f, mime_type='application/json')

    def send_movie_mkv(self, movie_id):
        if movie_id not in self.library.movies:
            return self.send_error(HTTPStatus.NOT_FOUND)

        filename = movie_id.split('/')[1] + '.mkv'

        path = self.library.movies[movie_id]['path']

        with path.open(mode='rb') as f:
            self.send_fileobj(f, mime_type='video/x-matroska')

    def send_movie_attachment(self, movie_id, attachment_name):
        # Find the specified mkv
        if movie_id not in self.library.movies:
            return self.send_error(HTTPStatus.NOT_FOUND)
        path = self.library.movies[movie_id]['path']

        # Get the list of attachments on this mkv
        identify_result = subprocess.run(['mkvmerge', '--identify', path.as_posix()], capture_output=True)
        if identify_result.returncode != 0:
            return self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

        # Find the attachment by name
        for line in identify_result.stdout.decode('utf-8').split('\n'):
            m = re.match(r"Attachment ID (?P<attachment_id>[0-9]+): type '(?P<mime_type>[^']+)',(.*,?) file name '(?P<filename>[^']+)'.*", line)
            if m and m.group('filename') == attachment_name:
                break
        else:
            return self.send_error(HTTPStatus.NOT_FOUND)

        # Extract the pertinent information from the metadata line
        mime_type = m.group('mime_type')
        attachment_id = m.group('attachment_id')
        filename = m.group('filename')

        # Extact the attachment content
        extract_result = subprocess.run(['mkvextract', '--quiet', path.as_posix(), 'attachments', f'{attachment_id}:/dev/stdout'], capture_output=True)
        if extract_result.returncode != 0:
            return self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

        # Serve the attachment content
        self.send_fileobj(io.BytesIO(extract_result.stdout), mime_type=mime_type)


def main():
    args = parse_args()

    logging.basicConfig(level=logging.INFO)

    listen_address = (args.listen_address, args.listen_port)

    # Init the library
    library = Library(args.base_dir)
    library.scan()

    handler_factory = HandlerFactory(library=library, allowed_clients=args.allow_ip)

    with http.server.ThreadingHTTPServer(listen_address, handler_factory.request_handler) as httpd:
        logging.info('serving at %s', listen_address)
        Handler.ALLOWED_CLIENT_ADDRESSES = args.allow_ip
        httpd.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
