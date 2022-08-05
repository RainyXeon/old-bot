import asyncio
import datetime as dt
import enum
import random
import re
import typing as t
from enum import Enum

import aiohttp
import discord
import wavelink
from discord.ext import commands

URL_REGEX = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"
LYRICS_URL = "https://some-random-api.ml/lyrics?title="
HZ_BANDS = (20, 40, 63, 100, 150, 250, 400, 450, 630, 1000, 1600, 2500, 4000, 10000, 16000)
TIME_REGEX = r"([0-9]{1,2})[:ms](([0-9]{1,2})s?)?"
OPTIONS = {
    "1️⃣": 0,
    "2⃣": 1,
    "3⃣": 2,
    "4⃣": 3,
    "5⃣": 4,
}


class AlreadyConnectedToChannel(commands.CommandError):
    pass


class NoVoiceChannel(commands.CommandError):
    pass


class QueueIsEmpty(commands.CommandError):
    pass


class NoTracksFound(commands.CommandError):
    pass


class PlayerIsAlreadyPaused(commands.CommandError):
    pass


class NoMoreTracks(commands.CommandError):
    pass


class NoPreviousTracks(commands.CommandError):
    pass


class InvalidRepeatMode(commands.CommandError):
    pass


class VolumeTooLow(commands.CommandError):
    pass


class VolumeTooHigh(commands.CommandError):
    pass


class MaxVolume(commands.CommandError):
    pass


class MinVolume(commands.CommandError):
    pass


class NoLyricsFound(commands.CommandError):
    pass


class InvalidEQPreset(commands.CommandError):
    pass


class NonExistentEQBand(commands.CommandError):
    pass


class EQGainOutOfBounds(commands.CommandError):
    pass


class InvalidTimeString(commands.CommandError):
    pass


class RepeatMode(Enum):
    NONE = 0
    ONE = 1
    ALL = 2


class Queue:
    def __init__(self):
        self._queue = []
        self.position = 0
        self.repeat_mode = RepeatMode.NONE

    @property
    def is_empty(self):
        return not self._queue

    @property
    def current_track(self):
        if not self._queue:
            raise QueueIsEmpty

        if self.position <= len(self._queue) - 1:
            return self._queue[self.position]

    @property
    def upcoming(self):
        if not self._queue:
            raise QueueIsEmpty

        return self._queue[self.position + 1:]

    @property
    def history(self):
        if not self._queue:
            raise QueueIsEmpty

        return self._queue[:self.position]

    @property
    def length(self):
        return len(self._queue)

    def add(self, *args):
        self._queue.extend(args)

    def get_next_track(self):
        if not self._queue:
            raise QueueIsEmpty

        self.position += 1

        if self.position < 0:
            return None
        elif self.position > len(self._queue) - 1:
            if self.repeat_mode == RepeatMode.ALL:
                self.position = 0
            else:
                return None

        return self._queue[self.position]

    def shuffle(self):
        if not self._queue:
            raise QueueIsEmpty

        upcoming = self.upcoming
        random.shuffle(upcoming)
        self._queue = self._queue[:self.position + 1]
        self._queue.extend(upcoming)

    def set_repeat_mode(self, mode):
        if mode == "none":
            self.repeat_mode = RepeatMode.NONE
        elif mode == "1":
            self.repeat_mode = RepeatMode.ONE
        elif mode == "all":
            self.repeat_mode = RepeatMode.ALL

    def empty(self):
        self._queue.clear()
        self.position = 0


class Player(wavelink.Player):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = Queue()
        self.eq_levels = [0.] * 15

    async def connect(self, ctx, channel=None):
        if self.is_connected:
            raise AlreadyConnectedToChannel

        if (channel := getattr(ctx.author.voice, "channel", channel)) is None:
            raise NoVoiceChannel

        await super().connect(channel.id)
        return channel

    async def teardown(self):
        try:
            await self.destroy()
        except KeyError:
            pass

    async def add_tracks(self, ctx, tracks):
        if not tracks:
            raise NoTracksFound

        if isinstance(tracks, wavelink.TrackPlaylist):
            self.queue.add(*tracks.tracks)
        elif len(tracks) == 1:
            self.queue.add(tracks[0])
            await ctx.send(f"**✅ Added {tracks[0].title} to the queue.**")
        else:
            if (track := await self.choose_track(ctx, tracks)) is not None:
                self.queue.add(track)
                await ctx.send(f"**✅ Added {track.title} to the queue.**")

        if not self.is_playing and not self.queue.is_empty:
            await self.start_playback()

    async def choose_track(self, ctx, tracks):
        def _check(r, u):
            return (
                r.emoji in OPTIONS.keys()
                and u == ctx.author
                and r.message.id == msg.id
            )

        embed = discord.Embed(
            title="▶ Choose a song",
            description=(
                "\n".join(
                    f"**{i+1}.** {t.title} ({t.length//60000}:{str(t.length%60).zfill(2)})"
                    for i, t in enumerate(tracks[:5])
                )
            ),
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="Query Results")
        embed.set_footer(text=f"Invoked by {ctx.author.display_name}", icon_url=ctx.author.avatar_url)

        msg = await ctx.send(embed=embed)
        for emoji in list(OPTIONS.keys())[:min(len(tracks), len(OPTIONS))]:
            await msg.add_reaction(emoji)

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=60.0, check=_check)
        except asyncio.TimeoutError:
            await msg.delete()
            await ctx.message.delete()
        else:
            await msg.delete()
            return tracks[OPTIONS[reaction.emoji]]

    async def start_playback(self):
        await self.play(self.queue.current_track)

    async def advance(self):
        try:
            if (track := self.queue.get_next_track()) is not None:
                await self.play(track)
        except QueueIsEmpty:
            pass

    async def repeat_track(self):
        await self.play(self.queue.current_track)


class Music(commands.Cog, wavelink.WavelinkMixin):
    def __init__(self, bot):
        self.bot = bot
        self.wavelink = wavelink.Client(bot=bot)
        self.bot.loop.create_task(self.start_nodes())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not member.bot and after.channel is None:
            if not [m for m in before.channel.members if not m.bot]:
                await self.get_player(member.guild).teardown()

    @wavelink.WavelinkMixin.listener()
    async def on_node_ready(self, node):
        print(f" Wavelink node `{node.identifier}` ready.")

    @wavelink.WavelinkMixin.listener("on_track_stuck")
    @wavelink.WavelinkMixin.listener("on_track_end")
    @wavelink.WavelinkMixin.listener("on_track_exception")
    async def on_player_stop(self, node, payload):
        if payload.player.queue.repeat_mode == RepeatMode.ONE:
            await payload.player.repeat_track()
        else:
            await payload.player.advance()

    async def cog_check(self, ctx):
        if isinstance(ctx.channel, discord.DMChannel):
            await ctx.send("**❎ Music commands are not available in DMs.**")
            return False

        return True

    async def start_nodes(self):
        await self.bot.wait_until_ready()

        nodes = {
            "MAIN": {
                "host": "disbotlistlavalink.ml",
                "port": 443,
                "rest_uri": "https://disbotlistlavalink.ml:443",
                "password": "LAVA",
                "identifier": "MAIN",
                "region": "europe",
                "secure": True,
            }
        }

        for node in nodes.values():
            await self.wavelink.initiate_node(**node)

    def get_player(self, obj):
        if isinstance(obj, commands.Context):
            return self.wavelink.get_player(obj.guild.id, cls=Player, context=obj)
        elif isinstance(obj, discord.Guild):
            return self.wavelink.get_player(obj.id, cls=Player)

    @commands.command(name="join", aliases=["connect"])
    async def connect_command(self, ctx, *, channel: t.Optional[discord.VoiceChannel]):
        player = self.get_player(ctx)
        channel = await player.connect(ctx, channel)
        await ctx.send(f"**✅ RainyMusic™ is connected to {channel.name}.**")

    @connect_command.error
    async def connect_command_error(self, ctx, exc):
        if isinstance(exc, AlreadyConnectedToChannel):
            await ctx.send("**✅ RainyMusic™ is already connected to a voice channel.**")
        elif isinstance(exc, NoVoiceChannel):
            await ctx.send("**❎ No suitable voice channel was provided.**")

    @commands.command(name="leave", aliases=["disconnect"])
    async def disconnect_command(self, ctx):
        player = self.get_player(ctx)
        await player.teardown()
        await ctx.send("**✅ RainyMusic™ is disconnected... **")

    @commands.command(name="play")
    async def play_command(self, ctx, *, query: t.Optional[str]):
        player = self.get_player(ctx)

        if not player.is_connected:
            await player.connect(ctx)

        if query is None:
            if player.queue.is_empty:
                raise QueueIsEmpty

            await player.set_pause(False)
            await ctx.send("**▶ Playback resumed.**")

        else:
            query = query.strip("<>")
            if not re.match(URL_REGEX, query):
                query = f"ytsearch:{query}"

            await player.add_tracks(ctx, await self.wavelink.get_tracks(query))

    @play_command.error
    async def play_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**❎ No songs to play as the queue is empty.**")
        elif isinstance(exc, NoVoiceChannel):
            await ctx.send("**❎ No suitable voice channel was provided.**")

    @commands.command(name="pause")
    async def pause_command(self, ctx):
        player = self.get_player(ctx)

        if player.is_paused:
            raise PlayerIsAlreadyPaused

        await player.set_pause(True)
        await ctx.send("**⏸ Playback paused.**")

    @pause_command.error
    async def pause_command_error(self, ctx, exc):
        if isinstance(exc, PlayerIsAlreadyPaused):
            await ctx.send("**⏸ It's already paused!**")

    @commands.command(name="stop")
    async def stop_command(self, ctx):
        player = self.get_player(ctx)
        player.queue.empty()
        await player.stop()
        await ctx.send("**⏹ Playback stopped.**")

    @commands.command(name="skip", aliases=["next"])
    async def next_command(self, ctx):
        player = self.get_player(ctx)

        if not player.queue.upcoming:
            raise NoMoreTracks

        await player.stop()
        await ctx.send("**⏭ Playing next track in queue.**")

    @next_command.error
    async def next_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 This could not be executed as the queue is currently empty.**")
        elif isinstance(exc, NoMoreTracks):
            await ctx.send("**😥 There are no more tracks in the queue.**")

    @commands.command(name="previous")
    async def previous_command(self, ctx):
        player = self.get_player(ctx)

        if not player.queue.history:
            raise NoPreviousTracks

        player.queue.position -= 2
        await player.stop()
        await ctx.send("**⏮ Playing previous track in queue.**")

    @previous_command.error
    async def previous_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 This could not be executed as the queue is currently empty.**")
        elif isinstance(exc, NoPreviousTracks):
            await ctx.send("**😥 There are no previous tracks in the queue.**")

    @commands.command(name="shuffle")
    async def shuffle_command(self, ctx):
        player = self.get_player(ctx)
        player.queue.shuffle()
        await ctx.send("**🔀 Queue shuffled.**")

    @shuffle_command.error
    async def shuffle_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 The queue could not be shuffled as it is currently empty.**")

    @commands.command(name="loop")
    async def repeat_command(self, ctx, mode: str):
        if mode not in ("none", "1", "all"):
            await ctx.send(f"The loop mode cannot be run. Maybe you got the syntax wrong, so check the syntax of loop mode by typing r!help.")
            raise InvalidRepeatMode

        player = self.get_player(ctx)
        player.queue.set_repeat_mode(mode)
        await ctx.send(f"**🔁 The loop mode has been set to {mode}.**")

    @commands.command(name="queue")
    async def queue_command(self, ctx, show: t.Optional[int] = 10):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        embed = discord.Embed(
            title="Queue",
            description=f"Showing up to next {show} tracks",
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="Query Results")
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.avatar_url)
        embed.add_field(
            name="Currently playing",
            value=getattr(player.queue.current_track, "title", "No tracks currently playing."),
            inline=False
        )
        if upcoming := player.queue.upcoming:
            embed.add_field(
                name="Next up",
                value="\n".join(t.title for t in upcoming[:show]),
                inline=False
            )

        msg = await ctx.send(embed=embed)

    @queue_command.error
    async def queue_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 The queue is currently empty.**")

    # Requests -----------------------------------------------------------------

    @commands.group(name="volume", invoke_without_command=True)
    async def volume_group(self, ctx, volume: int):
        player = self.get_player(ctx)

        if volume < 0:
            raise VolumeTooLow

        if volume > 150:
            raise VolumeTooHigh

        await player.set_volume(volume)
        await ctx.send(f"**✅ Volume set to {volume:,}%**")

    @volume_group.error
    async def volume_group_error(self, ctx, exc):
        if isinstance(exc, VolumeTooLow):
            await ctx.send("**⚠ The volume must be 0% or above. If the sound is too low, you won't hear anything!**")
        elif isinstance(exc, VolumeTooHigh):
            await ctx.send("**⚠ The volume must be 150% or below. If the sound is too high, your ears will be affected!**")

    @volume_group.command(name="up")
    async def volume_up_command(self, ctx):
        player = self.get_player(ctx)

        if player.volume == 150:
            raise MaxVolume

        await player.set_volume(value := min(player.volume + 10, 150))
        await ctx.send(f"**🔼 Volume set to {value:,}%**")

    @volume_up_command.error
    async def volume_up_command_error(self, ctx, exc):
        if isinstance(exc, MaxVolume):
            await ctx.send("**⚠ The player is already at max volume. The sound is too loud ;-;**")

    @volume_group.command(name="down")
    async def volume_down_command(self, ctx):
        player = self.get_player(ctx)

        if player.volume == 0:
            raise MinVolume

        await player.set_volume(value := max(0, player.volume - 10))
        await ctx.send(f"**🔽 Volume set to {value:,}%**")

    @volume_down_command.error
    async def volume_down_command_error(self, ctx, exc):
        if isinstance(exc, MinVolume):
            await ctx.send("**⚠ The player is already at min volume. Don't hear anything ;-;**")

    @commands.command(name="lyrics")
    async def lyrics_command(self, ctx, name: t.Optional[str]):
        player = self.get_player(ctx)
        name = name or player.queue.current_track.title

        async with ctx.typing():
            async with aiohttp.request("GET", LYRICS_URL + name, headers={}) as r:
                if not 200 <= r.status <= 299:
                    raise NoLyricsFound

                data = await r.json()

                if len(data["lyrics"]) > 2000:
                    return await ctx.send(f"<{data['links']['genius']}>")

                embed = discord.Embed(
                    title=data["title"],
                    description=data["lyrics"],
                    colour=ctx.author.colour,
                    timestamp=dt.datetime.utcnow(),
                )
                embed.set_thumbnail(url=data["thumbnail"]["genius"])
                embed.set_author(name=data["author"])
                await ctx.send(embed=embed)

    @lyrics_command.error
    async def lyrics_command_error(self, ctx, exc):
        if isinstance(exc, NoLyricsFound):
            await ctx.send("**❎ Cannot find lyrics!**")

    @commands.command(name="eq")
    async def eq_command(self, ctx, preset: str):
        player = self.get_player(ctx)

        eq = getattr(wavelink.eqs.Equalizer, preset, None)
        if not eq:
            raise InvalidEQPreset

        await player.set_eq(eq())
        await ctx.send(f"**✅ Equaliser adjusted to the {preset} preset.**")

    @eq_command.error
    async def eq_command_error(self, ctx, exc):
        if isinstance(exc, InvalidEQPreset):
            await ctx.send("**⚠ The EQ preset must be either 'flat', 'boost', 'metal', or 'piano'.**")

    @commands.command(name="adveq", aliases=["aeq"])
    async def adveq_command(self, ctx, band: int, gain: float):
        player = self.get_player(ctx)

        if not 1 <= band <= 15 and band not in HZ_BANDS:
            raise NonExistentEQBand

        if band > 15:
            band = HZ_BANDS.index(band) + 1

        if abs(gain) > 10:
            raise EQGainOutOfBounds

        player.eq_levels[band - 1] = gain / 10
        eq = wavelink.eqs.Equalizer(levels=[(i, gain) for i, gain in enumerate(player.eq_levels)])
        await player.set_eq(eq)
        await ctx.send("**✅ Equaliser adjusted.**")

    @adveq_command.error
    async def adveq_command_error(self, ctx, exc):
        if isinstance(exc, NonExistentEQBand):
            await ctx.send(
                "**This is a 15 band equaliser -- the band number should be between 1 and 15, or one of the following **"
                "**frequencies: **" + "**, **".join(str(b) for b in HZ_BANDS)
            )
        elif isinstance(exc, EQGainOutOfBounds):
            await ctx.send("**⚠ The EQ gain for any band should be between 10 dB and -10 dB.**")

    @commands.command(name="playing", aliases=["np"])
    async def playing_command(self, ctx):
        player = self.get_player(ctx)

        if not player.is_playing:
            raise PlayerIsAlreadyPaused

        embed = discord.Embed(
            title="⏯ Now playing",
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow(),
        )
        embed.set_author(name="Playback Information")
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.avatar_url)
        embed.add_field(name="Track title", value=player.queue.current_track.title, inline=False)
        embed.add_field(name="Artist", value=player.queue.current_track.author, inline=False)

        position = divmod(player.position, 60000)
        length = divmod(player.queue.current_track.length, 60000)
        embed.add_field(
            name="Position",
            value=f"{int(position[0])}:{round(position[1]/1000):02}/{int(length[0])}:{round(length[1]/1000):02}",
            inline=False
        )

        await ctx.send(embed=embed)

    @playing_command.error
    async def playing_command_error(self, ctx, exc):
        if isinstance(exc, PlayerIsAlreadyPaused):
            await ctx.send("**😥 There is no track currently playing.**")

    @commands.command(name="skipto", aliases=["playindex"])
    async def skipto_command(self, ctx, index: int):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        if not 0 <= index <= player.queue.length:
            raise NoMoreTracks

        player.queue.position = index - 2
        await player.stop()
        await ctx.send(f"**⏯ Playing track in position {index}.**")

    @skipto_command.error
    async def skipto_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 There are no tracks in the queue.**")
        elif isinstance(exc, NoMoreTracks):
            await ctx.send("**😥 That index is out of the bounds of the queue.**")

    @commands.command(name="restart")
    async def restart_command(self, ctx):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        await player.seek(0)
        await ctx.send("**🔄 Track restarted.**")

    @restart_command.error
    async def restart_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("**😥 There are no tracks in the queue.**")

    @commands.command(name="seek")
    async def seek_command(self, ctx, position: str):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        if not (match := re.match(TIME_REGEX, position)):
            raise InvalidTimeString

        if match.group(3):
            secs = (int(match.group(1)) * 60) + (int(match.group(3)))
        else:
            secs = int(match.group(1))

        await player.seek(secs * 1000)
        await ctx.send("**✅ Seeked.**")

    @commands.command(name="help")
    async def help(self, ctx):
        embed = discord.Embed(
            title="Wellcome to RainyMusic™! This is our user guide!",
            description=(
                "**-------------------------[Introduction and usage]-------------------------**\n"
                + "***In this user guide we will give you information about the command name, its uses and effects.***\n"
                + "**The way to read the instructions is as follows according to this example:**\n"
                + "**Play: r!play + url **\n"
                + "**Play music from the given URL. See supported sources below.**\n"
                + "***In there:***\n"
                + "> **Play: here is the command name**\n"
                + "> **r!play: here is the command syntax**\n"
                + "> **Play music from the given URL. See supported sources below.: here are explanations and instructions.**\n"
                + "**Good luck! ;D**\n"
                + "**-------------------------[User Guide]-------------------------**\n"
            ),
            colour=0x3B87F6,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="RainyMusic™'s user guide", icon_url="https://cdn.discordapp.com/app-icons/933352277501161532/f52d7928fe342d2eef850d64bab1121d.png")
        embed.add_field(
            name = '___***Play: r!play + url***___', 
            value= "**Play music from the given URL. Resume track.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Eq: r!eq + name eq template***___', 
            value= "**Adjust the sound used, change the volume balance of the frequency range.** \n__**Supported eq: flat, boost, metal, and piano**__", 
            inline = False
            )

        embed.add_field(
            name = '___***Join: r!join***___', 
            value= "**Join the voice channel you're in.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Leave: r!leave***___', 
            value= "**Leave the voice channel you're in.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Stop: r!stop***___', 
            value= "**Stop the music you are playing and let the bot leave the conversation channel.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Shuffle: r!shuffle***___', 
            value= "**Toggle shuffle mode.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Repeat: r!loop***___', 
            value= "**Change the loop mode.**\n**Repeat mode that supported: none, all, 1**", 
            inline = False
            )

        embed.add_field(
            name = '___***Playing: r!playing***___', 
            value= "**Display the currently playing track.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Pause: r!pause***___', 
            value= "**Pause the player.**", 
            inline = False
            )
        embed.add_field(
            name = '___***Next: r!next***___', 
            value= "**Playing the next track from the queue. **", 
            inline = False
            )

        embed.add_field(
            name = '____***Skipto: r!skipto + number***____', 
            value= "**Play the next track but in the requested sequence number.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Previous: r!previous ***___', 
            value= "**Playing the previous track from the queue. **", 
            inline = False
            )
        embed.add_field(
            name = '___***Lyrics: r!lyrics***___', 
            value= "**Show the lyrics of the currently playing song.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Queue: r!queue***___', 
            value= "**Display the queue of the current tracks in the playlist.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Volume: r!volume + numbers***___', 
            value= "**Set the volume**\n**Maximum allowed volume: 150**\n**Minimum allowed volume: 0**", 
            inline = False
            )

        embed.add_field(
            name = '___***Restart: r!restart***___', 
            value= "___***Restart the currently playing track.***___", 
            inline = False
            )

        embed.add_field(
            name = '___***Seek: r!seek + time example: 4:10:0***___', 
            value= "**Set the position of the track to the given time.**", 
            inline = False
            )

        embed.add_field(
            name = '___***Adveq: r!adveq + number***___', 
            value= "**Adjust equaliser **", 
            inline = False
            )

        embed.add_field(
            name = '___***Play name: r!play + name***___', 
            value= "**Search for a track on YouTube**", 
            inline = False
            )
        embed.set_footer(text=f"Help me ;-; This user guide is soo long ;-; And please don't copyrighted my bot ;-; XeonDex </>#0017")

        msg = await ctx.send(embed=embed)

    @commands.command(name="ミクダヨー", aliases=["mikudayo"])
    async def mikudayo_command(self, ctx):
        embed = discord.Embed(
            title="Congratulations on finding easter egg #1, click the link to find out what it is.",
            description=(
                "**https://www.youtube.com/watch?v=uX0QXQZdbuo**"
            ),
            colour=0x1BD6EB,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="Easter Egg #1", icon_url="https://cdn.discordapp.com/app-icons/933352277501161532/f52d7928fe342d2eef850d64bab1121d.png")

        msg = await ctx.send(embed=embed)

def setup(bot):
    bot.add_cog(Music(bot))
