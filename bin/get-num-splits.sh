#!/bin/bash

if [ $# == 0 ]; then
    echo "Usage: $0 <url> [<url> [...]]"
    exit 1
fi

while [ $# != 0 ]; do
    url=$1;
    shift;
    statsdataUrl=$(echo $url |sed 's/\?anonymousKey/statsdata.json\?anonymousKey/g')
    curl -s $statsdataUrl | jq '.SMT.VCSpace.[] | .totalSplits' |head -1
done
