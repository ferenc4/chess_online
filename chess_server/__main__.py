from chess_server.api.impl.ws_server import WsServer


def main():
    WsServer().start("localhost", 8765)


if __name__ == '__main__':
    main()
