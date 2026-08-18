"""DEPRECATED - inspects the ISD csv headers in yearly_files_csv/.

Those files come from the retired ISD source and are no longer refreshed. GHCNh
publishes typed parquet, so header probing like this is unnecessary.
"""
import os
import csv

CURRENT_FILES = os.listdir('yearly_files_csv/2025')

DATA_CAPTURE = list()

CONDITION_COUNT = 0

for item in CURRENT_FILES:
	with open(f'yearly_files_csv/2025/{item}', 'r') as file_reader:
		reader = csv.reader(file_reader)
		for idx, row in enumerate(reader):
			if idx == 0:
				DATA_CAPTURE.append([item, row])
				if 'AA1' in row:
					CONDITION_COUNT += 1 


for record in DATA_CAPTURE:
	print(record)

print(CONDITION_COUNT)
