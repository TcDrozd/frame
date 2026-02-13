## Full bucket
python3 s3_tree.py --bucket trevor-shared-photo-stream

## Limit Depth
python3 s3_tree.py --bucket trevor-shared-photo-stream -L 2

## Only under photos/
python3 s3_tree.py --bucket trevor-shared-photo-stream --prefix photos/ -L 3
