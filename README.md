# webnews-redaccao

Ajudante em Python usado pelas tarefas agendadas da redacção automática dos portais WebNews.ao (agro, petro, money, tech, life e sport).

`wn_wp.py` cria matérias no WordPress **sempre como Pendentes** (a aguardar revisão), carrega e enquadra imagens e verifica que nada fica publicado.

As credenciais nunca ficam neste repositório: o ajudante lê-as das variáveis de ambiente `WN_USER` e `WN_APP_PASS`.
