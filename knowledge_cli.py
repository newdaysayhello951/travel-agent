"""导入本机namespace的JSON资料；不抓取任意URL。用户资料建议使用认证API。"""
import argparse,json
from pathlib import Path
from config import CONFIG
from storage import PlanStore
from knowledge import KnowledgeStore

def main():
    parser=argparse.ArgumentParser();parser.add_argument('file');parser.add_argument('--replace',help='更新已有本机文档ID')
    args=parser.parse_args();path=Path(args.file)
    if path.stat().st_size>200000:raise ValueError('资料文件过大')
    from api import DocumentInput
    body=DocumentInput.model_validate(json.loads(path.read_text(encoding='utf-8')))
    store=PlanStore()
    try:print(json.dumps({'document_id':KnowledgeStore(store).put(**body.model_dump(mode='json'),document_id=args.replace)},ensure_ascii=False))
    finally:store.close()

if __name__=='__main__':main()
