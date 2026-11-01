# The O-RAN-SC O1 simulator, simulated O-DU O1 (sim-o1-ofhmp-interfaces, "PyNTS"), built from its m-release source (ADR 0012, plan task S0.15).
#
# Upstream publishes no image and has no release tag: its Makefile builds `pynts-base` from base/Dockerfile and then `pynts-o-du-o1` from
# o-du-o1/Dockerfile (FROM pynts-base:latest), on the developer's machine. This file is those two Dockerfiles in one (Apache-2.0, copyright
# highstreet technologies and others; the LICENSES file of the source), with three changes and nothing else:
#   1. the base image is pinned by digest (deploy/mirror/images.txt lists it, so CI pulls it from the GHCR mirror);
#   2. every file comes from the named build context `pynts`, the upstream repository at the pinned m-release commit (docker-compose.yml and
#      .github/workflows/oran-sim-image.yml pass it), instead of the build context;
#   3. the simulator's data directory (o-du-o1/data, which upstream bind-mounts at /data) is copied into the image, so the image runs on its own
#      in compose and in the chart;
#   4. its NETCONF server configuration (/data/ietf-netconf-server-running.json) is ours: upstream's has only a TLS endpoint, whose server
#      certificate the simulator makes at start-up for the name `client.smo` rather than its own address, so a client that checks the host
#      name (RAN NF OAM's does, always) cannot use it. Ours adds an SSH endpoint on 830 (user netconf, upstream's password `netconf!`) next to the
#      same TLS endpoint, and the simulator's pnfRegistration announces both (ADR 0012 section 5).
# The `# syntax=` line of upstream's files is left out on purpose: it makes BuildKit pull docker/dockerfile:1, an unpinned image.
#
# What is not pinned, as upstream builds it: the Ubuntu packages (apt), the Python packages (pip, unversioned requirements files), the libyang,
# sysrepo, libnetconf2 and netopeer2 sources (git tags; libyang-python is a commit), and the 3GPP YANG models (a forge.3gpp.org archive of tag
# Tag_Rel18_SA103). Two builds of the same commit can therefore differ; ADR 0012 section 3 says why that is accepted for a test simulator.

FROM ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11

# ---------------------------------------------------------------- base/Dockerfile (pynts-base)
RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
  tzdata build-essential git cmake pkg-config \
  libpcre2-dev \
  libssh-dev libssl-dev libcurl4-openssl-dev libpam-dev \
  python3-dev python3-cffi python3-pip \
  supervisor psmisc \
  unzip \
  openssl openssh-client openssh-server \
  vsftpd \
  libpcap-dev tcpdump iproute2 \
  && \
  apt-get clean && \
  rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/*

# the NETCONF user, also the SFTP and FTP user of the PM files
RUN \
  adduser --system netconf && \
  groupadd -f netconf && \
  echo "netconf:netconf!" | chpasswd

RUN \
  mkdir /ftp && \
  chown -R netconf /ftp && \
  mkdir /var/run/vsftpd && \
  mkdir /var/run/vsftpd/empty  && \
  mkdir /run/sshd && \
  echo "Match User netconf\n    ChrootDirectory /\n    X11Forwarding no\n    AllowTcpForwarding no\n    ForceCommand internal-sftp -d /ftp" >> /etc/ssh/sshd_config

COPY --from=pynts base/docker/conf/vsftpd.conf /etc/vsftpd.conf
COPY --from=pynts base/docker/conf/vsftpd.userlist /etc/vsftpd.userlist

RUN mkdir /opt/dev
WORKDIR /opt/dev

RUN \
  git config --global advice.detachedHead false && \
  git clone --single-branch --branch v3.4.2 https://github.com/CESNET/libyang.git && \
  git clone --single-branch --branch master https://github.com/CESNET/libyang-python.git && \
  git clone --single-branch --branch v2.11.7 https://github.com/sysrepo/sysrepo.git && \
  git clone --single-branch --branch v1.7.3 https://github.com/sysrepo/sysrepo-python.git && \
  git clone --single-branch --branch v3.5.1 https://github.com/CESNET/libnetconf2.git && \
  git clone --single-branch --branch v2.2.31 https://github.com/CESNET/netopeer2.git

RUN \
  cd libyang && \
  mkdir build && cd build  && \
  cmake -DCMAKE_BUILD_TYPE:String="Debug" -DENABLE_BUILD_TESTS=OFF .. && \
  make -j4  && \
  make install && \
  ldconfig

RUN \
  cd libyang-python && \
  git reset --hard 012d144 && \
  pip3 install .

COPY --from=pynts base/docker/conf/pynts-sysrepo.patch /opt/dev/sysrepo/pynts-sysrepo.patch
RUN \
  cd sysrepo && \
  git apply pynts-sysrepo.patch && \
  mkdir build && cd build  && \
  cmake -DCMAKE_BUILD_TYPE:String="Debug" -DENABLE_TESTS=OFF -DNACM_RECOVERY_USER=root .. && \
  make -j4 && \
  make install && \
  ldconfig

COPY --from=pynts base/docker/conf/pynts-sysrepo-python.patch /opt/dev/sysrepo-python/pynts-sysrepo-python.patch
RUN \
  cd sysrepo-python && \
  git apply pynts-sysrepo-python.patch && \
  pip3 install .

RUN \
  cd libnetconf2 && \
  mkdir build && cd build && \
  cmake -DCMAKE_BUILD_TYPE:String="Debug" -DENABLE_BUILD_TESTS=OFF .. && \
  make -j4 && \
  make install && \
  ldconfig

COPY --from=pynts base/docker/conf/pynts-netopeer2.patch /opt/dev/netopeer2/pynts-netopeer2.patch
RUN \
  cd netopeer2 && \
  git apply pynts-netopeer2.patch && \
  mkdir build && cd build && \
  cmake -DCMAKE_BUILD_TYPE:String="Debug" -DSYSREPO_SETUP=OFF .. && \
  make -j4 && \
  make install

ENV NP2_MODULE_PERMS=600
ENV NP2_MODULE_DIR=/usr/local/share/yang/modules/netopeer2/
ENV LN2_MODULE_DIR=/usr/local/share/yang/modules/libnetconf2/
ENV PYNTS_MODULE_DIR=/usr/local/share/yang/modules/pynts/

# upstream's test PKI (the O-DU's and an SMO's CA, key and certificate), part of the source: a lab simulator's, never a real secret
COPY --from=pynts base/docker/conf/ODU_rootCA.key /home/oranuser/.ssh/ODU_rootCA.key
COPY --from=pynts base/docker/conf/ODU_rootCA.crt /home/oranuser/.ssh/ODU_rootCA.crt
COPY --from=pynts base/docker/conf/ODU_client.key /home/oranuser/.ssh/ODU_client.key
COPY --from=pynts base/docker/conf/ODU_client.crt /home/oranuser/.ssh/ODU_client.crt
COPY --from=pynts base/docker/conf/SMO_rootCA.key /home/oranuser/.ssh/SMO_rootCA.key
COPY --from=pynts base/docker/conf/SMO_rootCA.crt /home/oranuser/.ssh/SMO_rootCA.crt
COPY --from=pynts base/docker/conf/end_odu_openssl.cnf /home/oranuser/.ssh/end_odu_openssl.cnf

COPY --from=pynts base/docker/yang /usr/local/share/yang/modules/pynts/
COPY --from=pynts base/docker/scripts /opt/dev/scripts
RUN \
  /opt/dev/scripts/setup.sh

COPY --from=pynts base/src /app
RUN pip install -r /app/requirements.txt

ENV NETCONF_USERNAME="netconf"
ENV NETCONF_PASSWORD="netconf!"

# ---------------------------------------------------------------- o-du-o1/Dockerfile (pynts-o-du-o1)
RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
  wget && \
  apt-get clean && \
  rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/*

COPY --from=pynts o-du-o1/docker/yang /usr/local/share/yang/modules/pynts
COPY --from=pynts o-du-o1/docker/scripts/install_yangs.sh /opt/dev/scripts/install_yangs.sh

# the 3GPP YANG models, downloaded from forge.3gpp.org (tag Tag_Rel18_SA103), then installed into sysrepo
COPY --from=pynts o-du-o1/docker/scripts/get_yangs.sh /opt/dev/scripts/get_yangs.sh
RUN \
  /opt/dev/scripts/get_yangs.sh

RUN \
  /opt/dev/scripts/install_yangs.sh

COPY --from=pynts o-du-o1/docker/conf/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY --from=pynts o-du-o1/src /app/extensions/o-du-o1
RUN pip install -r /app/extensions/o-du-o1/requirements.txt

# change 3: the data upstream mounts at /data (the ManagedElement, the NETCONF server, the PM template and its 60-second log period)
COPY --from=pynts o-du-o1/data /data
# change 4: an SSH listen endpoint on 830 beside upstream's TLS one
COPY ietf-netconf-server-running.json /data/ietf-netconf-server-running.json

WORKDIR /app

ENV NETWORK_FUNCTION_TYPE="o-du-o1"

EXPOSE 830
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
