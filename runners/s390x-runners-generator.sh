#!/bin/bash

while !(ping -q -c 1 -W 1 google.com >/dev/null)
do
    echo "waiting for internet connectivity..."
    sleep 2
done

# Regular runners
for i in {1..4}
do
    mkdir keda-s390x-$i
    cp -r template/* keda-s390x-$i
    cd keda-s390x-$i
    pwd
    echo HOME=$(pwd) > .env
    ./config.sh --url https://github.com/kedacore --token TOKEN_HERE --name keda-s390x-$i --replace --unattended

    sudo ./svc.sh install
    sudo ./svc.sh start
    cd ..
done
