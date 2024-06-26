# wb-device-manager

### Запуск сканирования
Для запуска сканирования необходимо выполнить MQTT RPC запрос `wb-device-manager/bus-scan/Start/client_id`.

Структура параметров запроса
```jsonc
{
    // Тип сканирования:
    //  - "extended" - только используя быстрый Modbus
    //  - "standard" - только используя прямой перебор адресов
    // Если не задан, то производится сначала сканирование через быстрый Modbus, а потом прямой перебор адресов
    "scan_type": "extended",

    // Не удалять результаты прошлого сканирования.
    // Если не задан, результаты прошлого сканирования удаляются
    "preserve_old_results": false,

    // Порт, на котором необходимо провести сканирование.
    // Если не задан, то производится сканирование на всех доступных портах
    "port": {

        // Системный путь до устройства порта
        "path": "/dev/ttyRS485-2"
    }
}
```

В качестве ответа в топике `wb-device-manager/bus-scan/Start/client_id/reply` будет опубликовано сообщение c результатом `Ok` при успешном запуске сканирования. Пока сканирование выполняется, следующие запросы к `wb-device-manager/bus-scan/Start/client_id` будут возвращать mqtt-rpc ошибку с кодом -33100.

### Остановка сканирования
Для остановки сканирования необходимо выполнить MQTT RPC запрос `wb-device-manager/bus-scan/Stop/client_id`.

В качестве ответа в топике `wb-device-manager/bus-scan/Stop/client_id/reply` будет опубликовано сообщение c результатом `Ok` при успешной остановке сканирования. Если сканирование не выполняется, запросы к `wb-device-manager/bus-scan/Stop/client_id` возвращают mqtt-rpc ошибку с кодом -33100.



### Результат сканирования

Структура данных в топике списка устройств (`/wb-device-manager/state`):

```jsonc
{
    // сервис выполняет сканирование портов
    "scanning": true,

    // прогресс завершения сканирования портов
    "progress": 50,

    // порты (с настройками связи), на которых в данный момент идёт сканирование
    "scanning_ports": ["/dev/ttyRS485-1 9600 8N1", "/dev/ttyRS485-2 9600 8N1"],

    // сканирование производится через быстрый modbus или нет
    "is_ext_scan": false,

    // ошибка, не относящаяся к конкретному устройству
    // (например, rpc-timeout при неработающем wb-mqtt-serial)
    "error": {
        // принятый внутри команды идентификатор сообщения (формат строго определён!)
        "id": "com.wb.device_manager.rpc_call_timeout_error",
        // fallback человекочитаемое сообщение
        "message": "RPC call to wb-mqtt-serial timed out. Check, wb-mqtt-serial is running"
    },

    // список устройств
    "devices" : [
        {
            // название устройства (для людей)
            "title": "MR6C",

            // для обращения к устройству; формируется при первом сканировании устройства
            "uuid": "9b5cbc0a-24b3-3065-b105-c999b0293a97",

            // серийный номер устройства
            "sn": "13453ghh",

            // название устройства (для внутреннего использования)
            "device_signature": "WBMR6C",

            // сигнатура прошивки (для внутреннего использования)
            "fw_signature": "mr6cG",

            // устройство доступно и отвечает на запросы
            "online": true,

            // устройство опрашивается через wb-mqtt-serial
            // в текущей итерации не реализовано со стороны wb-mqtt-serial; всегда true
            "poll": true,

            // unix ts последнего сканирования устройства
            "last_seen": 1668154795454,

            // устройство в режиме загрузчика
            "bootloader_mode": true,

            // список ошибок работы с конкретным устройством
            "errors": [
                {
                    "id": "com.wb.device_manager.device.read_device_signature_error",
                    "message": "Failed to read device signature."
                },
                {
                    "id": "com.wb.device_manager.device.read_fw_signature_error",
                    "message": "Failed to read FW signature."
                }
            ],

            // slave_id одинаковый с кем-то еще (флаг выставляется у всех устройств с таким же slave_id)
            "slave_id_collision": true,

            // порт, к которому подключено устройство
            "port": {

                // системный путь до устройства порта
                "path": "/dev/ttyRS485-2"
            },

            // текущие настройки устройства
            "cfg": {
                // адрес
                "slave_id": 100,

                // скорость шины
                "baud_rate": 9600,

                // чётность
                "parity": "N",

                // число бит данных
                "data_bits": 8,

                // число стоп бит
                "stop_bits": 2
            },

            // прошивка устройства
            "fw": {
                // версия
                "version": "1.2.3",

                // поддерживает ли прошивка быстрое сканирование через extended modbus
                "ext_support": false,

                "update": {
                    // процент завершения процесса обновления прошивки
                    "progress": 50,

                    // последняя ошибка обновления прошивки конкретного устройства
                    "error": { // пока не используется; обновление fw не реализовано
                        // принятый внутри команды идентификатор сообщения (формат строго определён!)
                        "id": "com.wb.device_manager.fw_update_error",
                        // fallback человекочитаемое сообщение
                        "message": "FW update failed. Check logs for more info"
                    },

                    // Актуальная версия прошивки (для текущего релиза)
                    "available_fw": "2.2.2"
                }
            }
        },
        ...
    ]
}
```

### Работа с ошибками
* поле ```error.id``` имеет строго определённый формат: ```com.wb.название_пакета.тип_ошибки```
* ```error.metadata``` содержит дополнительную информацию, в зависимости от типа ошибки
* подробные ошибки из питона (со stack trace) доступны в логах (```journalctl -u wb-device-manager -f```)

#### Ошибки, выдаваемые наружу:
| Id | Условия возникновения | поле ```"metadata"``` |
| :- | :-------------------- | :-------------- |
| **com.wb.device_manager.generic_error** | Неотловленная ошибка внутри сервиса | ```null``` |
| Наследники: |
| **com.wb.device_manager.rpc_call_timeout_error** | Таймаут rpc-запроса к wb-mqtt-serial (wb-device-manager - клиент) на этапе получения портов для сканирования | ```null``` |
| **com.wb.device_manager.failed_to_scan_error** | Неотловленная ошибка при сканировании порта | ```"failed_ports" : [failed_port1, failed_port2, ...]``` |
| **com.wb.device_manager.device.read_fw_version_error** | Ошибка modbus-коммуникации с устройством (чтение fw_version) | ```null``` |
| **com.wb.device_manager.device.read_fw_signature_error** | Ошибка modbus-коммуникации с устройством (чтение fw_signature) | ```null``` |
| **com.wb.device_manager.device.read_device_signature_error** | Ошибка modbus-коммуникации с устройством (чтение device_signature) | ```null``` |
| **com.wb.device_manager.device.read_serial_params_error** | Ошибка modbus-коммуникации с устройством (чтение serial-настроек; актуально для tcp портов) | ```null``` |
