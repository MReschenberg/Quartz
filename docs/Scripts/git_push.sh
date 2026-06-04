#! /bin/bash

cd "/Users/morganraereschenberg/Library/Mobile Documents/iCloud~md~obsidian/Documents"

# Date in format Day-Month-Year
date=$(date +"%Y-%m-%d %T")

# Commit message
message="Commit for $date"
git add -A
git commit -m"${message}"
status="$(git status --branch --porcelain)"
echo $status >> ~/cron_echo.txt
if [ "$status" == "## master...origin/master" ]; then
  echo "IT IS CLEAN" >> ~/cron_echo.txt
else
  echo "There is stuff to push" >> ~/cron_echo.txt
  git push
fi
