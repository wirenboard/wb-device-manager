# wb-device-manager

Структура данных в топике списка устройств:

```jsonc
{
    // сервис выполняет сканирование портов
    "scanning": true,

    // список устройств
    "devices" : [
        {
            // название устройства (для людей)
            "title": "MR6C",

            // для обращения к устройству; формируется при первом сканировании устройства
            "uuid": "bb2b49a5",

            // серийный номер устройства
            "sn": "13453ghh",

            // название устройства (для внутреннего использования)
            "device_signature": "WBMR6C",

            // сигнатура прошивки (для внутреннего использования)
            "fw_signature": "mr6cG",

            // устройство доступно и отвечает на запросы
            "online": true,

            // устройство опрашивается через wb-mqtt-serial
            "poll": true,

            // unix ts последнего сканирования устройства
            "last_scan": 1668154795454,

            // устройство в режиме загрузчика
            "bootloader_mode": true,

            // текст последней ошибки при работе с устройством
            "error": "Error message",

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

                "update": {
                    // процент завершения процесса обновления прошивки
                    "progress": 50,

                    // текст последней ошибки обновления прошивки
                    "error": "Error message",

                    // Актуальная версия прошивки (для текущего релиза)
                    "available_fw": "2.2.2"
                }
            }
        },
        ...
    ]
}
```
