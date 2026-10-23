"""命令行入口：python -m pifa_lingshou request.json --output response.json。"""
from .service.request_runner import main


if __name__ == '__main__':
    main()
