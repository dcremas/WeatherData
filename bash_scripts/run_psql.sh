#/bin/zsh

# A place to run postgres sequel scripts from commands.

time_start=$(date +%s)

psql --host=127.0.0.1 --port=15432 --dbname=weatherdata -f sql/analytics_slp_decrease.sql

time_stop=$(date +%s)

diff=$(( time_stop - time_start ))

echo "The script took $diff seconds to complete."

exit 0
