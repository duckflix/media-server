FROM ubuntu:20.04
RUN apt-get -y update
RUN apt-get -y install python3 python3-pip
RUN apt-get -y install ca-certificates
COPY gpg-pub-moritzbunkus.gpg /usr/share/keyrings/gpg-pub-moritzbunkus.gpg
COPY mkvtoolnix.download.list /etc/apt/sources.list.d/mkvtoolnix.download.list
RUN apt-get -y update
RUN apt-get -y install mkvtoolnix
RUN python3 -m pip install aiofiles fastapi uvicorn
COPY app.py /app/app.py
USER nobody
WORKDIR /app
ENTRYPOINT ["python3", "-u", "./app.py", "/media/"]
