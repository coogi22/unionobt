import os
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
from aiohttp import ClientTimeout
from datetime import datetime, timezone
import traceback

from utils.supabase import get_supabase

# -----------------------------
# CONFIG
# -----------------------------
GUILD_ID = 1345153296360542271
ACCESS_ROLE_ID = 1444450052323147826
LOG_CHANNEL_ID = 1449252986911068273

SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")

supabase = get_supabase()


# -----------------------------
# SELLAUTH HELPERS
# -----------------------------
async def fetch_invoice(invoice_id: str) -> dict | None:
    if not SELLAUTH_API_KEY or not SELLAUTH_SHOP_ID:
        return None

    url = f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/invoices/{invoice_id}"
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}"}

    timeout = ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


def invoice_is_paid(invoice: dict) -> bool:
    status = (invoice.get("status") or "").lower()
    refunded = bool(invoice.get("refunded", False))
    cancelled = bool(invoice.get("cancelled", False))
    return status in {"completed", "paid"} and not refunded and not cancelled


# -----------------------------
# COG
# -----------------------------
class InvoiceRedeem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="redeem",
        description="Verify a SellAuth order/invoice ID and grant access to a selected user"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    # Optional: uncomment to restrict to admins by default
    @app_commands.default_permissions(administrator=True)
    async def redeem(
        self,
        interaction: discord.Interaction,
        order_id: str,
        user: discord.Member
    ):
        # Defer immediately so Discord doesn't timeout
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            return

        try:
            invoice_id = (order_id or "").strip()


            # Basic config
            if not SELLAUTH_API_KEY or not SELLAUTH_SHOP_ID:
                await interaction.followup.send("SellAuth is not configured yet.", ephemeral=True)
                return

            guild = interaction.guild
            if not guild:
                await interaction.followup.send("This command must be used in the server.", ephemeral=True)
                return

            # Safety: ensure target user is in this guild
            if user.guild.id != guild.id:
                await interaction.followup.send("That user is not in this server.", ephemeral=True)
                return

            # 1) Already redeemed?
            existing = (
                supabase.table("role_redeem")
                .select("id")
                .eq("invoice_id", invoice_id)
                .execute()
            )
            if existing.data:
                await interaction.followup.send("This order has already been redeemed.", ephemeral=True)
                return

            # 2) Verify with SellAuth
            invoice = await fetch_invoice(invoice_id)
            if not invoice:
                await interaction.followup.send("Order not found. Double-check the ID and try again.", ephemeral=True)
                return

            if not invoice_is_paid(invoice):
                await interaction.followup.send(
                    "This order is not completed/paid, or it was refunded/cancelled.",
                    ephemeral=True
                )
                return

            product_name = invoice.get("product_name", "Unknown Product")
            variant_name = invoice.get("variant_name")

            # 3) Give role to the SELECTED user
            role = guild.get_role(ACCESS_ROLE_ID)
            if not role:
                await interaction.followup.send("Access role not found. Contact staff.", ephemeral=True)
                return

            if role not in user.roles:
                try:
                    await user.add_roles(role, reason=f"SellAuth redeem {invoice_id} (issued by {interaction.user.id})")
                except discord.Forbidden:
                    await interaction.followup.send(
                        "I can’t assign roles. Make sure my role is above the access role and I have Manage Roles.",
                        ephemeral=True
                    )
                    return

            # 4) Store redemption (records who redeemed AND who received it)
            supabase.table("role_redeem").insert({
                "code": None,
                "role_id": int(ACCESS_ROLE_ID),
                "redeemed": True,
                "redeemed_by": int(interaction.user.id),   # staff who ran /redeem
                "invoice_id": invoice_id,
                "product_name": product_name,
                "variant_name": variant_name,
                "discord_username": str(user),            # who received access
                "redeemed_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            # 5) Log
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if log_channel is None:
                try:
                    log_channel = await guild.fetch_channel(LOG_CHANNEL_ID)
                except Exception:
                    log_channel = None

            if log_channel:
                embed = discord.Embed(title="Order Redeemed", color=discord.Color.orange())
                embed.add_field(name="Staff", value=f"{interaction.user} ({interaction.user.id})", inline=False)
                embed.add_field(name="Granted To", value=f"{user} ({user.id})", inline=False)
                embed.add_field(name="Product", value=product_name, inline=False)
                embed.add_field(name="Variant", value=variant_name or "N/A", inline=False)
                embed.add_field(name="Order ID", value=invoice_id, inline=False)
                await log_channel.send(embed=embed)

            await interaction.followup.send(
                f"Confirmed order and granted access to {user.mention}.",
                ephemeral=True
            )

        except Exception as e:
            print("ERROR in /redeem:", repr(e))
            traceback.print_exc()
            await interaction.followup.send(
                "Something broke while redeeming. Staff: check bot terminal logs.",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(InvoiceRedeem(bot))
    print("✅ Loaded cog: invoice_redeem")  
