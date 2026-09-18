"""Validate the planning package, not implementation or operational readiness."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.workflow.contracts import WorkflowHandoff
from core.workflow.readiness import ReadinessGate
from core.roadmap.service import build_repository_roadmap_service
PACKAGE = ROOT / '.factory/planning/continuous-autonomy'
DOCS = ROOT / 'docs/handoffs/continuous-autonomy'
def digest(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding='utf-8-sig').encode('utf-8')).hexdigest()
def artifacts() -> list[Path]:
    paths = list(DOCS.glob('*.md')) + [ROOT/'docs/CONTINUOUS_AUTONOMY_PLAN_2026-09-18.md',ROOT/'docs/ROADMAP_OPERACIONAL.md',ROOT/'docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md',ROOT/'docs/handoffs/HF-05.md',ROOT/'.factory/roadmap/darkfac.json',PACKAGE/'plan.json',PACKAGE/'initial-handoff.json',PACKAGE/'source-review.md',PACKAGE/'baseline-sources.json',Path(__file__)]
    return sorted(paths)
def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest',action='store_true')
    parser.add_argument('--binding')
    args=parser.parse_args()
    plan=json.loads((PACKAGE/'plan.json').read_text(encoding='utf-8-sig'))
    units=plan['units']; ids={u['ticket_id'] for u in units}
    assert len(ids)==len(units)==33, 'duplicate or unexpected unit count'
    if args.binding:
        unit=next(u for u in units if u['ticket_id']==args.binding)
        for path in unit['allowed_paths']:
            candidate=ROOT/path
            assert candidate.is_file() and candidate.stat().st_size>0, f'missing binding/result: {path}'
        print('BINDING_STRUCTURE_ONLY; requires semantic review and real probes; no readiness approval')
        return 0
    visited=set(); visiting=set()
    by_id={u['ticket_id']:u for u in units}
    def visit(id: str) -> None:
        assert id not in visiting, 'cycle: '+id
        if id in visited:return
        visiting.add(id)
        for dep in by_id[id]['depends_on']:
            assert dep in ids, 'orphan: '+dep
            visit(dep)
        visiting.remove(id); visited.add(id)
    for id in sorted(ids):visit(id)
    missing_baseline_paths=[]
    baseline_sources=json.loads((PACKAGE/'baseline-sources.json').read_text(encoding='utf-8'))['sources']
    def ancestors(id):
        return {dep for direct in by_id[id]['depends_on'] for dep in ({direct} | ancestors(direct))}
    for u in units:
        assert len(u['allowed_paths'])<=4
        assert (ROOT/u['handoff_ref']).is_file()
        assert u['implementation_status']=='not_started' and u['operational_status']=='not_verified'
        assert u['successors']==[v['ticket_id'] for v in units if u['ticket_id'] in v['depends_on']]
        if u['new_test']:assert u['new_test'] in u['new_paths'], 'new test must be marked new'
        for path in u['allowed_paths']:
            assert not Path(path).is_absolute() and '..' not in Path(path).parts
            if path not in u['new_paths'] and not (ROOT/path).exists():
                assert path in baseline_sources and 'HF-26-03' in ancestors(u['ticket_id']), 'unbound missing source: '+path
                missing_baseline_paths.append({'ticket_id':u['ticket_id'],'path':path,'blocking_binding':'HF-26-03'})
    h=WorkflowHandoff.model_validate_json((PACKAGE/'initial-handoff.json').read_text(encoding='utf-8'))
    result=ReadinessGate().evaluate(h)
    assert not result.eligible, 'no trusted context must not authorize dispatch'
    assert any('CONTEXT_REQUIRED' in r for r in result.reasons)
    docs=[*DOCS.glob('*.md'),ROOT/'docs/CONTINUOUS_AUTONOMY_PLAN_2026-09-18.md']
    checked_links=0
    for doc in docs:
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)',doc.read_text(encoding='utf-8-sig')):
            if re.match(r'^[a-z]+:',link) or link.startswith('#'):continue
            target=link.split('#',1)[0]
            if target:
                assert (doc.parent/target).resolve().exists(), f'broken link {doc.name}: {link}'
                checked_links+=1
    service=build_repository_roadmap_service(ROOT,include_hf=True,include_infra=True)
    snapshot=service.get_snapshot('darkfac')
    projected={i.id:i for i in snapshot.items}
    for u in units:
        item=projected[u['ticket_id']]
        assert item.delivery_status.value=='planned', f'false completion {item.id}'
        assert not item.evidence_refs
        assert sorted(d.item_id for d in item.dependencies)==sorted(u['depends_on'])
    assert projected['HF-26'].delivery_status.value=='planned'
    assert [d.item_id for d in projected['HF-26'].dependencies]==['HF-15-02']
    new_issues=[i.model_dump(mode='json') for i in snapshot.issues if set(i.item_ids)&(ids|{'HF-26'})]
    assert not [i for i in new_issues if i['code'] in {'causal_cycle','orphan_dependency','conflicting_state'}], new_issues
    manifest_path=PACKAGE/'integrity.json'
    actual={p.relative_to(ROOT).as_posix():digest(p) for p in artifacts()}
    if args.write_manifest:
        manifest_path.write_text(json.dumps(dict(schema_version='1',purpose='integrity_not_approval',hash_mode='UTF-8 text normalized LF without BOM',artifacts=actual),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    else:
        expected=json.loads(manifest_path.read_text(encoding='utf-8'))['artifacts']
        assert actual==expected, 'integrity mismatch'
    result=dict(units=len(units),edges=sum(len(u['depends_on']) for u in units),projected_new_items=len(ids)+1,links_checked=checked_links,new_scope_issues=new_issues,ready_without_dependencies=[u['ticket_id'] for u in units if not u['depends_on']],workflow_handoff_schema='valid',gate_without_context='correctly_rejected',runtime_approval='not_attested',missing_baseline_paths=missing_baseline_paths,scope='documentary_not_operational')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0
if __name__=='__main__':
    raise SystemExit(main())
