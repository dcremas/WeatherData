#!/bin/zsh

<<-"Comment"
The purpose of this script is to do a record count of the .csv files for a
particular year after the processing/inserting of data in the database
to ensure that the number of records in the observations table matches what
is pulled out of the files.
Comment

# The year is the only command line argument that needs to be processed.
YEAR=$1

NEW_PATH="/Users/dustincremascoli/Documents/website/WeatherData/yearly_files_csv/$YEAR"
cd $NEW_PATH

FILE_COUNT=0
RECORD_COUNT=0

for file in *.csv
do
  (( FILE_COUNT++ ))
  TEMP_COUNT=$( egrep -c '(FM-12|FM-15)' $file )
  RECORD_COUNT=$(( RECORD_COUNT + TEMP_COUNT ))
  echo "The current file count is: $TEMP_COUNT, and the current total record count is: $RECORD_COUNT"
done

echo "The total file count is: $FILE_COUNT"
echo "The total record count for all files is $RECORD_COUNT"

exit 0
