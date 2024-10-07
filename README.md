# wb-device-manager

### Запуск сканирования
Для запуска сканирования необходимо выполнить MQTT RPC запрос `wb-device-manager/bus-scan/Start/client_id`.

Структура параметров запроса
```jsonc
{
    // Тип сканирования:
    //  - "extended" - только используя быстрый Modbus
    //  - "standard" - только используя прямой перебор адресов
    //  - "bootloader" - поиск устройств, находящихся в режиме загрузчика
    // Если не задан, то производится сначала сканирование через быстрый Modbus, а потом прямой перебор адресов
    "scan_type": "extended",

    // Не удалять результаты прошлого сканирования.
    // Если не задан, результаты прошлого сканирования удаляются
    "preserve_old_results": false,

    // Порт, на котором необходимо провести сканирование.
    // Если не задан, то производится сканирование на всех доступных портах
    "port": {

        // Системный путь до устройства порта, либо путь для шлюза в формате IP_АДРЕС:ПОРТ
        "path": "/dev/ttyRS485-2"
    },

    // Адреса устройств, поиск которых будет производиться в первую очередь
    // Используется при поиске устройств в режиме загрузчика
    "out_of_order_slave_ids": [ 1234 ]
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

            // порт, к которому подключено устройство
            "port": {

                // системный путь до устройства порта, либо путь для шлюза в формате IP_АДРЕС:ПОРТ
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
                "ext_support": false
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


### Запрос текущей и доступной для обновления прошивок 
MQTT RPC запрос `wb-device-manager/fw-update/GetFirmwareInfo/client_id`.

Структура параметров запроса
```jsonc
{
    // Адрес устройства. Обязательный парамер
    "slave_id": 123,

    // Настройки последовательного порта
    "port": {
        // Системный путь до устройства порта. Обязательный параметр
        "path": "/dev/ttyRS485-1",
    
        // Скорость порта. Если не задано, будет использоваться 9600
        "baud_rate": 9600,
    
        // Чётность - N, O или E. Если не задано, будет использоваться N
        "parity": "N",
    
        // Количество стоп-бит.  Если не задано, будет использоваться 2
        "stop_bits": 2
    },

    // Либо настройки доступа к шлюзу
    "port": {
        // IP адрес или доменное имя шлюза
        "address": "1.1.1.1",

        // Порт шлюза
        "port": 12345
    }
}
```

В качестве ответа в топике `wb-device-manager/fw-update/GetFirmwareInfo/client_id/reply` будет опубликована структура с данными
```jsonc
{
    // Текущая прошивка устройства
    "fw": "1.2.3",

    // Прошивка, доступная для обновления. 
    // Если пустая строка, то устройство не поддерживает обновление прошивки
    "available_fw": "2.3.5",

    // Признак того, что прошивка может быть обновлена средствами wb-device-manager через RPC fw-update/Update
    "can_update": false
}
```

### Обновление прошивки
Для запуска обновления прошивки необходимо выполнить MQTT RPC запрос `wb-device-manager/fw-update/Update/client_id`.

Структура параметров запроса
```jsonc
{
    // Адрес устройства. Обязательный парамер
    "slave_id": 123,

    // Настройки последовательного порта
    "port": {
        // Системный путь до устройства порта. Обязательный параметр
        "path": "/dev/ttyRS485-1",
    
        // Скорость порта. Если не задано, будет использоваться 9600
        "baud_rate": 9600,
    
        // Чётность - N, O или E. Если не задано, будет использоваться N
        "parity": "N",
    
        // Количество стоп-бит.  Если не задано, будет использоваться 2
        "stop_bits": 2
    },

    // Либо настройки доступа к шлюзу
    "port": {
        // IP адрес или доменное имя шлюза
        "address": "1.1.1.1",

        // Порт шлюза
        "port": 12345
    }
}
```

В качестве ответа в топике `wb-device-manager/fw-update/Update/client_id/reply` будет опубликовано сообщение c результатом `Ok` при успешном запуске обновления. Пока обновление выполняется, следующие запросы к `wb-device-manager/fw-update/Update/client_id` будут возвращать mqtt-rpc ошибку с кодом -33100.

### Статус обновления прошивки

Структура данных в топике статуса обновления прошивки (`/wb-device-manager/firmware-update/state`):

```jsonc
{
    // список устройств, прошивка которых обновляется
    "devices" : [
        {
            // порт, к которому подключено устройство
            "port": {

                // системный путь до устройства порта, либо путь для шлюза в формате IP_АДРЕС:ПОРТ
                "path": "/dev/ttyRS485-2"
            },

            // адрес
            "slave_id": 100,

            // процент завершения процесса обновления прошивки
            "progress": 50,

            // текущая версия прошивки
            "from_fw": "1.0.0",

            // прошивка, на которую производится обновление
            "to_fw": "2.0.0",

            // последняя ошибка обновления прошивки конкретного устройства
            "error": {
                // человекочитаемое сообщение
                "message": "FW update failed. Check logs for more info"
            },
        },
        ...
    ]
}
```

### Сброс ошибок обновления прошивки

Если в процессе обновления прошивки возникла ошибка, будет опубликована соответствующая запись в топике статуса обновления. Эта ошибка будет публиковаться всегда до перезапуска сервиса wb-device-manager. Чтобы удалить ошибку из топика статуса без перезапуска сервиса, надо выполнить MQTT RPC запрос `wb-device-manager/fw-update/ClearError/client_id`.

Структура параметров запроса
```jsonc
{
    // Адрес устройства
    "slave_id": 123,

    "port": {
        // Системный путь до устройства порта, либо путь для шлюза в формате IP_АДРЕС:ПОРТ
        "path": "/dev/ttyRS485-1",
    }
}
```

В качестве ответа в топике `wb-device-manager/fw-update/ClearError/client_id/reply` будет опубликовано сообщение c результатом `Ok`

### Восстановление устройств, находящихся в режиме загрузчика
Если устройство находится в режиме загрузчика в него можно прошить актуальную прошивку, выполнив MQTT RPC запрос `wb-device-manager/fw-update/Restore/client_id`.

Структура параметров запроса
```jsonc
{
    // Адрес устройства. Обязательный парамер
    "slave_id": 123,

    // Настройки последовательного порта
    "port": {
        // Системный путь до устройства порта. Обязательный параметр
        "path": "/dev/ttyRS485-1",
    
        // Скорость порта. Если не задано, будет использоваться 9600
        "baud_rate": 9600,
    
        // Чётность - N, O или E. Если не задано, будет использоваться N
        "parity": "N",
    
        // Количество стоп-бит.  Если не задано, будет использоваться 2
        "stop_bits": 2
    },

    // Либо настройки доступа к шлюзу
    "port": {
        // IP адрес или доменное имя шлюза
        "address": "1.1.1.1",

        // Порт шлюза
        "port": 12345
    }
}
```

В качестве ответа в топике `wb-device-manager/fw-update/Restore/client_id/reply` будет опубликовано сообщение c результатом `Ok` при успешном запуске обновления. Пока обновление выполняется, следующие запросы к `wb-device-manager/fw-update/Restore/client_id` будут возвращать mqtt-rpc ошибку с кодом -33100.
Прогресс операции отражается в топике статуса обновления прошивки.