from bot import MusicBot


def main():
    bot = MusicBot()
    bot.remove_command('help')
    bot.run()


if __name__ == "__main__":
    main()