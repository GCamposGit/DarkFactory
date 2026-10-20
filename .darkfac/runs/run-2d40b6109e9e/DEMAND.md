# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Planning da esteira falha UnknownProjectError em tickets USR

## Problema
Em 20/10/2026 api/line/live mostrou run-5c1eeae3564c USR-315 com retries handler_error:UnknownProjectError unknown project USR. O run registra project_id darkfac mas o handler parece cair no ticket_id como projeto. Causa ainda nao comprovada; pode envolver worker antigo, lookup do store ou contexto. Consulta canonica open_line_store/get_run_status confirmou runs.project_id=darkfac para run-5c1eeae3564c; nao e project_id corrompido na linha runs.

## Jornada
Planning da esteira falha UnknownProjectError em tickets USR

## Fora de escopo
- Nao relaxar piso de cota, Gate G1 ou isolamento de projetos.

## Criterios de aceite
- Reproduzir contexto planning com project_id darkfac e ticket USR-315; resolver projeto registrado sem fallback incorreto.
- Provar retomada de run real sem duplicar ticket ja entregue e preservar isolamento multi-projeto.
