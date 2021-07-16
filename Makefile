build:
	docker build -t docker.io/duckflix/media-server .

release:
	docker push docker.io/duckflix/media-server

up:
	docker run --restart=always -v /srv/media/movies/:/media:ro -p 58080:8000 -d --name duckflix-media-server docker.io/duckflix/media-server

down:
	docker stop duckflix-media-server
	docker rm duckflix-media-server
