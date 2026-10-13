# Agenda Eletrônica RJ

Este repositório mantém um calendário iCalendar (.ics) que combina a agenda https://agendaeletronica.com.br/rj/ com https://melhoresfestasdorio.com.br/eletronico/. Eventos de fora do estado do Rio de Janeiro são descartados.

## Link público para assinatura

https://raw.githubusercontent.com/JuDiNamO/agenda-eletronica-rj/main/agenda-eletronica-rj.ics

Assine esse endereço por URL no Google Calendar, Apple Calendar ou Outlook. Não importe o arquivo local, pois uma cópia importada não recebe atualizações.

O GitHub Actions consulta as duas fontes diariamente às 04:17 no horário de Brasília e atualiza esse mesmo arquivo quando há mudanças. Eventos equivalentes nas duas páginas são deduplicados, dando prioridade aos dados já publicados pela Agenda Eletrônica. Os blocos iCalendar de eventos futuros que já estão no feed são preservados sem reescrever seus campos; novas festas são anexadas. Para conferir a execução, abra a aba Actions. Se a estrutura de qualquer fonte mudar, a extração pode precisar de ajuste em build_calendar.py.

## Qualidade e testes

A cada execução, o pipeline:
- consulta as duas páginas de origem e gera o feed;
- roda testes unitários e de integração com pytest;
- valida o arquivo com o parser iCalendar e confere campos obrigatórios, UIDs únicos, datas com fuso horário, links dos ingressos, ausência de erros de parsing e linhas dentro do limite de 75 octetos do RFC 5545;
- roda análise estática do Python com Ruff;
- deduplica eventos entre as fontes por data e título normalizado, mantendo a versão da Agenda Eletrônica quando o mesmo evento aparece nos dois sites;
- preserva os blocos dos eventos futuros já publicados e acrescenta somente entradas novas; só grava o arquivo se o calendário mudar. A gravação usa substituição atômica para evitar arquivos parciais, e uma falha em qualquer consulta não apaga o feed anterior.

Para executar os testes localmente:

```bash
python -m pip install -r requirements-dev.txt
python build_calendar.py
python -m pytest -q
python -m ruff check build_calendar.py tests
```

Para uma atualização manual, abra **Actions → Atualizar calendário da Agenda Eletrônica RJ → Run workflow**. O resultado e os logs ficam disponíveis no histórico de execuções.
