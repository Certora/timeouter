#!/bin/bash

usage="Usage: $0 [-p <rule name>] <url> [<url> [...]]"

usage_long="\
Script for fetching statsdata.json from the web server.\n\
\n\
 -h|--help             Print this message and exit\n\
 \n\
 -p|--parallel <name>  Assume that the run links are from\n\
                       -splitParallel and show entry for\n\
                       rule <name>\n
"


PARALLEL=false

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            printf -- "${usage_long}\nIn summary\n\n  $usage\n\n"
            exit 1
            ;;
        -p|--parallel)
            PARALLEL=true
            ruleName=$2
            shift;
            ;;
        -*)
            echo "Error: invalid option '$1'"
            exit 1
            ;;
        *)
            break
    esac
    shift;
done

while [ $# != 0 ]; do
    url=$1;
    shift;
    statsdataUrl=$(echo $url |sed -E 's!/?\?anonymousKey!\/statsdata.json\?anonymousKey!g')
    if [ $PARALLEL == "true" ]; then
        curl -s $statsdataUrl |jq ".$ruleName.ParallelSplitter"
    else
        curl -s $statsdataUrl |jq '.SMT.VCSpace.[]'
    fi
done
