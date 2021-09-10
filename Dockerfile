# Build the container image on Ubuntu 20.04
FROM ubuntu:20.04

# Update Ubuntu
# TODO: this may not always be the right thing todo. Is it? Do we want to automatically ingest upstream updates? Is there even another way?
RUN apt-get -y update

# Install python
RUN apt-get -y install python3 python3-pip
RUN apt-get -y install ca-certificates

# Install mkvtoolnix
COPY gpg-pub-moritzbunkus.gpg /usr/share/keyrings/gpg-pub-moritzbunkus.gpg # The GPG key for mkvtoolnix.download.list
COPY mkvtoolnix.download.list /etc/apt/sources.list.d/mkvtoolnix.download.list
RUN apt-get -y update
RUN apt-get -y install mkvtoolnix

# Install fastapi/uvicorn
RUN python3 -m pip install aiofiles fastapi uvicorn

# Install our app
COPY app.py /app/app.py

# "Secure" our app
USER nobody

# Setup the app to run on launch of the container
WORKDIR /app
ENTRYPOINT ["python3", "-u", "./app.py", "/media/"]
