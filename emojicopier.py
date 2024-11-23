import random
import re
import tomllib

from enum import Enum
from typing import Sequence, cast
from logging import getLogger
from urllib.parse import urlparse

from discord import (
    Asset,
    Client,
    Color,
    Embed,
    Guild,
    GuildSticker,
    HTTPException,
    Intents,
    Interaction,
    Member,
    Message,
    Emoji,
    PartialEmoji,
    Permissions,
    Role,
    SelectOption,
    ButtonStyle,
    User,
)
from discord.app_commands import (
    CommandTree,
    ContextMenu,
    AppCommandContext,
    AppInstallationType,
    Command,
    AppCommandError,
)
from discord.ui import View, Select, Button, button
from discord.utils import oauth_url

from zxcvbn import zxcvbn

Expression = Emoji | PartialEmoji | GuildSticker

password_strengths = (
    ("Terrible", Color.brand_red()),
    ("Poor", Color.brand_red()),
    ("Okay", Color.yellow()),
    ("Good", Color.brand_green()),
    ("Excellent", Color.brand_green()),
)

class ExpressionLocation(Enum):
    MESSAGE = None
    REACTION = "Reaction"
    STICKER = "Sticker"
    STATUS = "Status"
    BIO = "Bio"


class ExpressionSelect(Select):
    def __init__(
        self, *, expressions: Sequence[tuple[Expression, ExpressionLocation]], **kwargs
    ):
        super().__init__(
            options=[
                SelectOption(
                    label=f":{expression.name}:",
                    emoji=(
                        expression
                        if isinstance(expression, (Emoji, PartialEmoji))
                        else None
                    ),
                    description=location.value,
                    value=str(expression.id),
                )
                for expression, location in expressions
            ],
            max_values=min(25, len(expressions)),
            **kwargs,
        )
        self._expressions = {
            expression.id: (expression, location)
            for expression, location in expressions
        }
        self.selected: list[tuple[Expression, ExpressionLocation]] = []

    async def callback(self, interaction: Interaction):
        self.selected = [self._expressions[int(id)] for id in self.values]
        await interaction.response.defer()


class GuildSelect(Select):
    def __init__(self, *, guilds: list[Guild], **kwargs):
        super().__init__(
            options=[
                SelectOption(
                    label=guild.name,
                    description=guild.description,
                    emoji=random.choice(guild.emojis) if len(guild.emojis) else None,
                    value=str(guild.id),
                )
                for guild in guilds
            ],
            max_values=min(25, len(guilds)),
            **kwargs,
        )
        self._guilds = {guild.id: guild for guild in guilds}
        self.selected: list[Guild] = []

    async def callback(self, interaction: Interaction):
        self.selected = [self._guilds[int(id)] for id in self.values]
        await interaction.response.defer()


class CopyExpressionsView(View):
    def __init__(
        self,
        client: Client,
        expressions: Sequence[tuple[Expression, ExpressionLocation]],
        guilds: list[Guild],
    ):
        super().__init__()
        self.client = client
        self.logger = getLogger("discord.expressioncopier")
        self.expression_select = ExpressionSelect(
            expressions=expressions, placeholder="Select expressions to copy", row=0
        )
        self.guild_select = GuildSelect(
            guilds=guilds, placeholder="Select target servers", row=1
        )
        self.add_item(self.expression_select)
        self.add_item(self.guild_select)

    @button(label="Copy", style=ButtonStyle.primary, row=2)
    async def on_copy(self, interaction: Interaction, button: Button):
        self.stop()
        await interaction.response.defer()
        await (await interaction.original_response()).edit(
            embed=Embed(color=Color.brand_green(), title="Copying expressions"),
            view=None,
        )
        succeeded: list[tuple[Expression, Guild]] = []
        failed: list[tuple[Expression, Guild, HTTPException]] = []
        for guild in self.guild_select.selected:
            for expression, location in self.expression_select.selected:
                try:
                    if location == ExpressionLocation.STICKER:
                        assert isinstance(expression, GuildSticker)
                        await guild.create_sticker(
                            name=expression.name,
                            description=expression.description,
                            emoji=expression.emoji,
                            file=await expression.to_file(),
                            reason=f"Copying sticker (requested by @{interaction.user.name})",
                        )
                    else:
                        await guild.create_custom_emoji(
                            name=expression.name,
                            image=await expression.read(),
                            reason=f"Copying emoji (requested by @{interaction.user.name})",
                        )
                except HTTPException as e:
                    self.logger.warning(
                        f"Failed to copy {expression} to {guild}", exc_info=True
                    )
                    failed.append((expression, guild, e))
                else:
                    self.logger.info(f"Copied {expression} to {guild}")
                    succeeded.append((expression, guild))
        embeds = []
        if len(succeeded):
            embeds.append(
                Embed(
                    color=Color.brand_green(),
                    title=f"Successfully copied {len(succeeded)} {"expressions" if len(succeeded) != 1 else "expression"}",
                )
            )
        if len(failed):
            embeds.append(
                Embed(
                    color=Color.brand_red(),
                    title=f"Failed to copy {len(failed)} {"expressions" if len(failed) != 1 else "expression"}",
                    description="\n".join(
                        f"- \\:{expression.name}\\: to {guild.name}: {error.text} ({error.code})"
                        for expression, guild, error in failed
                    ),
                )
            )
        if not len(embeds):
            embeds.append(
                Embed(
                    color=Color.yellow(),
                    title="Please select some expressions and servers.",
                )
            )
        await (await interaction.original_response()).edit(embeds=embeds)


class ErrorHandlingCommandTree(CommandTree):
    async def on_error(
        self, interaction: Interaction[Client], error: AppCommandError
    ) -> None:
        await interaction.response.send_message(
            embed=Embed(
                color=Color.brand_red(),
                title="Catastrophic failure",
                description="An error occured inside Ideograbber. Whoops!",
            ),
            ephemeral=True,
        )
        return await super().on_error(interaction, error)


class EmojiCopier(Client):
    EMOJI_REGEX = re.compile(r"<(?P<anim>a)?:(?P<name>\w{2,}):(?P<id>\d+)>")
    permissions = Permissions(create_expressions=True, manage_expressions=True)

    def __init__(self):
        intents = Intents.default()
        intents.members = True
        super().__init__(intents=intents)

        self.tree = ErrorHandlingCommandTree(self)

        self.tree.add_command(
            ContextMenu(
                name="Extract expressions",
                callback=self.extract_expressions,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )
        self.tree.add_command(
            ContextMenu(
                name="Extract user assets",
                callback=self.extract_user_assets,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )
        self.tree.add_command(
            ContextMenu(
                name="Check password strength",
                callback=self.check_password_strength,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )
        # self.tree.add_command(Command(
        #     name="server-assets",
        #     description="Extract server branding assets",
        #     callback=self.extract_server_assets,
        #     allowed_contexts=AppCommandContext(guild=True, dm_channel=False, private_channel=False),
        #     allowed_installs=AppInstallationType(guild=True, user=True)
        # ))
        self.tree.add_command(
            Command(
                name="role-icon",
                description="Extract role icon",
                callback=self.extract_role_icon,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )
        self.tree.add_command(
            Command(
                name="install",
                description="Share a link to install Ideograbber!",
                callback=self.install,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )

        # self.tree.add_command(
        #     Command(
        #         name="copy",
        #         description="Copy expressions from this server",
        #         callback=self.copy_server_expressions,
        #         allowed_contexts=AppCommandContext(
        #             guild=True, dm_channel=False, private_channel=False
        #         ),
        #         allowed_installs=AppInstallationType(guild=True, user=True),
        #     )
        # )
        self.tree.add_command(
            ContextMenu(
                name="Copy expressions",
                callback=self.copy_expressions,
                allowed_contexts=AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
                allowed_installs=AppInstallationType(guild=True, user=True),
            )
        )

    async def setup_hook(self):
        await self.tree.sync()

    def emojis_in_string(self, string: str):
        return {
            PartialEmoji.with_state(
                self._get_state(),
                id=int(match.group("id")),
                name=match.group("name"),
                animated=match.group("anim") is not None,
            )
            for match in EmojiCopier.EMOJI_REGEX.finditer(string)
        }

    def reaction_emojis(self, message: Message):
        return {
            reaction.emoji
            for reaction in message.reactions
            if isinstance(reaction.emoji, (Emoji, PartialEmoji))
            and reaction.emoji.id is not None
        }

    def elegible_guilds_for_user(self, user: User | Member):
        for guild in user.mutual_guilds:
            assert (member := guild.get_member(user.id)) is not None
            if member.guild_permissions.create_expressions:
                yield guild

    def format_asset_link(self, asset: Asset):
        return f"[{urlparse(asset.url).path.split("/")[-1]}]({asset.url})"
    
    async def install(self, interaction: Interaction):
        await interaction.response.send_message(
            f"[click here to WIN BIG]({oauth_url(cast(int, self.application_id), permissions=self.permissions)})"
        )

    async def check_password_strength(self, interaction: Interaction, message: Message):
        results = zxcvbn(message.content)
        await interaction.response.send_message(
            embed=Embed(
                color=password_strengths[results["score"]][1],
                title=f"Password strength: {password_strengths[results["score"]][0]}",
            ).add_field(
                name="Approximate number of guesses", value=results["guesses"], inline=False
            ).add_field(
                name="Worst-case time to crack", value=results["crack_times_display"]["offline_fast_hashing_1e10_per_second"], inline=False
            )
        )

    async def extract_expressions(self, interaction: Interaction, message: Message):
        body_emojis = self.emojis_in_string(message.content)
        reaction_emojis = self.reaction_emojis(message)
        embeds = []
        if len(body_emojis):
            embeds.append(
                Embed(
                    color=Color.brand_green(),
                    title=f"{len(body_emojis)} {"emojis" if len(body_emojis) != 1 else "emoji"} in message content",
                    description="\n".join(
                        [
                            f"- {str(emoji)} [{emoji.name}]({emoji.url})"
                            for emoji in body_emojis
                        ]
                    ),
                )
            )
        if len(reaction_emojis):
            embeds.append(
                Embed(
                    color=Color.brand_green(),
                    title=f"{len(reaction_emojis)} {"reactions" if len(reaction_emojis) != 1 else "reaction"}",
                    description="\n".join(
                        [
                            f"- {str(emoji)} [{emoji.name}]({emoji.url})"
                            for emoji in reaction_emojis
                        ]
                    ),
                )
            )
        if len(message.stickers):
            embeds.append(
                Embed(
                    color=Color.brand_green(),
                    title=f"{len(message.stickers)} {"stickers" if len(message.stickers) != 1 else "sticker"}",
                    description="\n".join(
                        [
                            f"- [{sticker.name}]({sticker.url})"
                            for sticker in message.stickers
                        ]
                    ),
                )
            )
        if len(embeds):
            await interaction.response.send_message(embeds=embeds, ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="This message has no emoji, reactions, or stickers.",
                ),
                ephemeral=True,
            )

    async def extract_user_assets(self, interaction: Interaction, user: Member | User):
        full_user = await self.fetch_user(user.id)
        embed = Embed(
            title=f"Assets of @{full_user.name} ({full_user.display_name})",
            color=full_user.accent_color,
        ).add_field(
            name="Global avatar",
            value=(
                self.format_asset_link(full_user.avatar)
                if full_user.avatar is not None
                else None
            ),
            inline=False,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        if full_user.accent_color is not None:
            embed.add_field(name="Color", value=full_user.accent_color)
        if full_user.banner is not None:
            embed.add_field(
                name="Banner",
                value=self.format_asset_link(full_user.banner),
                inline=False,
            )
        if full_user.avatar_decoration is not None:
            embed.add_field(
                name="Avatar decoration",
                value=self.format_asset_link(full_user.avatar_decoration),
                inline=False,
            )
        if isinstance(user, Member):
            if user.guild_avatar is not None and user.guild_avatar != user.avatar:
                embed.add_field(
                    name="Server avatar",
                    value=self.format_asset_link(user.guild_avatar),
                    inline=False,
                )
            if user.display_icon is not None:
                embed.add_field(
                    name="Role icon",
                    value=(
                        self.format_asset_link(user.display_icon)
                        if isinstance(user.display_icon, Asset)
                        else user.display_icon
                    ),
                    inline=False,
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def extract_server_assets(self, interaction: Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="This command can only be used in a server.",
                ),
                ephemeral=True,
            )
        else:
            embed = Embed(
                color=Color.brand_green(),
                title=f"Assets of {guild.name}",
            ).add_field(
                name="Icon",
                value=(
                    self.format_asset_link(guild.icon)
                    if guild.icon is not None
                    else None
                ),
                inline=False,
            )
            embed.set_thumbnail(url=guild.icon.url if guild.icon is not None else None)
            if guild.banner is not None:
                embed.add_field(
                    name="Banner",
                    value=self.format_asset_link(guild.banner),
                    inline=False,
                )
            if guild.splash is not None:
                embed.add_field(
                    name="Invite splash",
                    value=self.format_asset_link(guild.splash),
                    inline=False,
                )
            if guild.discovery_splash is not None:
                embed.add_field(
                    name="Discovery splash",
                    value=self.format_asset_link(guild.discovery_splash),
                    inline=False,
                )

            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def extract_role_icon(self, interaction: Interaction, role: Role):
        await interaction.response.send_message(
            embed=Embed(color=Color.brand_green(), title=f"Assets for role {role.name}")
            .add_field(
                name="Icon",
                value=(
                    self.format_asset_link(role.display_icon)
                    if isinstance(role.display_icon, Asset)
                    else role.display_icon
                ),
            )
            .add_field(name="Color", value=role.color)
            .set_thumbnail(url=role.icon.url if role.icon is not None else None),
            ephemeral=True,
        )

    async def copy_expressions(self, interaction: Interaction, message: Message):
        body_emojis = self.emojis_in_string(message.content)
        reaction_emojis = self.reaction_emojis(message)
        expressions: list[tuple[Expression, ExpressionLocation]] = list(
            {(emoji, ExpressionLocation.MESSAGE) for emoji in body_emojis}
            | {(emoji, ExpressionLocation.REACTION) for emoji in reaction_emojis}
            | {
                (sticker, ExpressionLocation.STICKER)
                async for sticker in (
                    await sticker.fetch() for sticker in message.stickers
                )
                if isinstance(sticker, GuildSticker)
            }
        )
        elegible_guilds = list(self.elegible_guilds_for_user(interaction.user))
        if not len(expressions):
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="This message has no copyable emoji, reactions, or stickers.",
                ),
                ephemeral=True,
            )
        elif len(elegible_guilds):
            await interaction.response.send_message(
                view=CopyExpressionsView(self, expressions, elegible_guilds),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="You do not have the Create Expressions permission in any servers you share with this bot.",
                ),
                view=View().add_item(
                    Button(
                        style=ButtonStyle.link,
                        label="Invite Ideograbber to a server!",
                        url=oauth_url(cast(int, self.application_id), permissions=self.permissions),
                    )
                ),
                ephemeral=True,
            )

    async def copy_server_expressions(self, interaction: Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="This command can only be used in a server.",
                ),
                ephemeral=True,
            )
        elif interaction.guild not in self.guilds:
            await interaction.response.send_message(
                embed=Embed(
                    color=Color.brand_red(),
                    title="Ideograbber isn't in this server and can't see its emojis.",
                    description=(
                        "This is a limitation of Ideograbber's bot library, which will be fixed in the future."
                        " For now, go to your server's bot channel (if it has one),"
                        " send a message with the expressions you wish to copy, and use the context menu command to copy them."
                    ),
                ),
                ephemeral=True,
            )
        else:
            expressions = [
                (emoji, ExpressionLocation.MESSAGE)
                for emoji in interaction.guild.emojis
            ] + [
                (sticker, ExpressionLocation.STICKER)
                for sticker in interaction.guild.stickers
            ]
            elegible_guilds = list(self.elegible_guilds_for_user(interaction.user))
            if not len(expressions):
                await interaction.response.send_message(
                    embed=Embed(
                        color=Color.brand_red(),
                        title="This server has no copyable emoji, reactions, or stickers.",
                    ),
                    ephemeral=True,
                )
            elif len(elegible_guilds):
                await interaction.response.send_message(
                    view=CopyExpressionsView(self, expressions, elegible_guilds),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=Embed(
                        color=Color.brand_red(),
                        title="You do not have the Create Expressions permission in any servers you are in with this bot.",
                    ),
                    view=View().add_item(
                        Button(
                            style=ButtonStyle.link,
                            label="Invite Ideograbber to a server!",
                            url=oauth_url(cast(int, self.application_id), permissions=self.permissions),
                        )
                    ),
                    ephemeral=True,
                )

if __name__ == "__main__":
    with open("config.toml", "rb") as f:
        config = tomllib.load(f)
    EmojiCopier().run(config["token"])
