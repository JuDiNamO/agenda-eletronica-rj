# Agenda Eletrônica RJ — calendário por assinatura

Este repositório gera um feed iCalendar (.ics) com festas e links de ingressos da página https://agendaeletronica.com.br/rj/ para Google Calendar, Apple Calendar, Android e Outlook.

## Como funciona

- O GitHub Actions consulta a agenda diariamente às 04:17 no horário de Brasília.
- Os eventos futuros são extraídos com nome, data, hora de início, local informado pela fonte e URL do ingresso.
- O calendário é publicado pelo GitHub Pages. O feed anterior não deve ser substituído se a leitura falhar ou não forem reconhecidos eventos.
- A fonte pode informar apenas o horário de início; o gerador não inventa horário de término.

## Ativação inicial

1. Vá a Settings → Pages.
2. Em Build and deployment, selecione GitHub Actions como Source.
3. Vá à aba Actions, escolha “Atualizar calendário da Agenda Eletrônica RJ” e clique em Run workflow.
4. Aguarde a execução concluir e confira se o feed e a página pública abrem.

Depois de ativo, o feed será disponibilizado em:
https://JuDiNamO.github.io/agenda-eletronica-rj/agenda-eletronica-rj.ics

Página informativa:
https://JuDiNamO.github.io/agenda-eletronica-rj/

## Como assinar

Use o endereço .ics e escolha assinar/adicionar calendário por URL; importar um arquivo local é apenas uma cópia e não se atualiza.

- Google Calendar na web: Outros calendários → + → Do URL.
- Apple Calendar no Mac: Arquivo → Nova assinatura de calendário.
- iPhone/iPad: Ajustes → Apps → Calendário → Contas de Calendário → Adicionar Conta → Outra → Adicionar Calendário Assinado.
- Outlook na web: Calendário → Adicionar calendário → Assinar da Web.
- Android: assine pelo Google Calendar na web e habilite a sincronização da conta.

## Manutenção

- A atualização é diária, mas cada aplicativo determina quando consulta o feed e pode demorar a refletir mudanças.
- Para atualização manual, execute o workflow pela aba Actions.
- Se a fonte alterar o HTML ou bloquear o acesso automatizado, veja os logs da etapa “Consultar o site e gerar .ics”. O script falha para evitar a publicação de um calendário vazio; ajustes podem ser necessários em build_calendar.py.
