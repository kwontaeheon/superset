terry@tk-ubuntu:~/code/docker/superset/images$ docker ps |grep superset
d46436e8294c   apache/superset:5b79f84e1ba2feab10bc9b96fd786d82418b7b4a   "/usr/bin/docker-ent…"   41 minutes ago   Up 41 minutes (unhealthy)   8088/tcp                           superset_worker
ddfd2f9ada8e   apache/superset:5b79f84e1ba2feab10bc9b96fd786d82418b7b4a   "/usr/bin/docker-ent…"   41 minutes ago   Up 41 minutes (unhealthy)   8088/tcp                           superset_worker_beat
6ceb93c50a29   apache/superset:5b79f84e1ba2feab10bc9b96fd786d82418b7b4a   "/usr/bin/docker-ent…"   41 minutes ago   Up 41 minutes (healthy)     0.0.0.0:8088->8088/tcp             superset_app
7f8e611d81a7   node:14                                                    "docker-entrypoint.s…"   41 minutes ago   Up 41 minutes                                                  superset_node
639ebe9429e5   redis:6.2.1                                                "docker-entrypoint.s…"   41 minutes ago   Up 41 minutes               127.0.0.1:6379->6379/tcp           superset_cache
f87b51f52eb1   postgres:10                                                "docker-entrypoint.s…"   41 minutes ago   Up 41 minutes               127.0.0.1:5432->5432/tcp           superset_db
terry@tk-ubuntu:~/code/docker/superset/images$ docker commit 7f8e611d81a7 node:14-local
sha256:a35c447bc7a350673f5def1aae60a1ff9d57c22496be44670586fd4400c0ebef
terry@tk-ubuntu:~/code/docker/superset/images$ docker commit 639ebe9429e5 redis:6.2.1-local
sha256:d89950d1432dd9365a6e8c0dd421f1074395614a5409b0a69c88323867c9f7c3
terry@tk-ubuntu:~/code/docker/superset/images$ docker commit f87b51f52eb1 postgres:10-local
sha256:8d3d5ec505df05b31b2998e563d8ca9ca1d463699405af9e3285ca5278ba235a
terry@tk-ubuntu:~/code/docker/superset/images$ docker commit 6ceb93c50a29 apache/superset:5b79f84e-local
sha256:08b46ae01430eae6400bce672d1e090b1882b1f852e7602e52550310cb8a089b
terry@tk-ubuntu:~/code/docker/superset/images$ docker save node:14-local -o node-14.tar.gz 
terry@tk-ubuntu:~/code/docker/superset/images$ docker save redis:6.2.1-local -o redis-6.2.1.tar.gz
terry@tk-ubuntu:~/code/docker/superset/images$ docker save postgres:10-local -o postgres-10.tar.gz
terry@tk-ubuntu:~/code/docker/superset/images$ docker save apache/superset:5b79f84e-local -o superset-5b79f84e.tar.gz
