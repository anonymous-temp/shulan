#!/usr/bin/env bash

conda activate shulan

export PYTHONPATH
PYTHONPATH=$(pwd)

mkdir logs
mkdir outputs

#read -r -p '开发环境/测试环境/生产环境（d/t/p）: ' choice
#case $choice in
#    d )
#        cp settings/settings.dev.py settings.py
#        ;;
#    t )
#        cp settings/settings.test.py settings.py
#        ;;
#    p )
#        cp settings/settings.production.py settings.py
#        ;;
#    * )
#        echo '请输入 d/t/p'
#        ;;
#esac
