# Файлы для CLI

Помещайте сюда дополнительные CSV/JSON. Команды `make import` и `make replan` принимают только имя файла в этой папке.

Пример после реализации приложения:

```bash
make import FILE=new_requests.csv
make import-reference FILE=control.csv DATASET_ID=<uuid>
make replan PLAN_ID=<uuid> EVENT_FILE=urgent_event.json
```

Оригиналы организатора находятся в `data/source` и обрабатываются `make seed`/`make data-audit`.
