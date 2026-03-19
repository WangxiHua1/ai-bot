import telebot
import os
import io
from flask import Flask, request
from supabase import create_client
from dotenv import load_dotenv
from google import genai
from google.genai import types
import threading

load_dotenv()

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GM_ID = int(os.getenv("GM_ID", 0))
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")  # Railway 自动注入

bot = telebot.TeleBot(BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==================== 新版 Gemini SDK ====================
client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash-image"

user_cache = {}
histories = {}        # 存储每个用户的 chat 对象
active_cards = {}
pending_cards = {}

# ============== Flask 健康检查 + Webhook ==============
app = Flask(__name__)


@app.route('/')
def home():
    return "✅ DND剧本杀 Bot 正常运行！UptimeRobot 已监测到 UP"


@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram Webhook 入口（解决 409 冲突）"""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'ERROR', 400


# ============== 用户 & 钻石系统 ==============
def get_or_create_user(tg_id: int, username: str):
    if tg_id in user_cache:
        return user_cache[tg_id]
    res = supabase.table("users").select("*").eq("telegram_id", tg_id).execute()
    if res.data:
        user = res.data[0]
    else:
        user = {"telegram_id": tg_id, "username": username, "diamonds": 5000, "ai_level": 1}
        supabase.table("users").insert(user).execute()
    user_cache[tg_id] = user
    return user


def update_user(tg_id, **kwargs):
    if tg_id in user_cache:
        user_cache[tg_id].update(kwargs)
    supabase.table("users").update(kwargs).eq("telegram_id", tg_id).execute()


def deduct_diamonds(tg_id: int, cost: int) -> bool:
    user = get_or_create_user(tg_id, "")
    if user["diamonds"] < cost:
        return False
    update_user(tg_id, diamonds=user["diamonds"] - cost)
    return True


def get_system_prompt(level: int, card=None):
    base = f"你是专业的DND剧本杀 Dungeon Master（DM），等级{level}（越高剧情越复杂、细节越丰富、推理越智能）。\n"
    base += "用第二人称沉浸式叙事，推进主线剧情，描述NPC对话、场景氛围、感官细节、线索和悬疑转折。\n"
    base += "玩家是主角，保持公平、紧张刺激的剧本杀氛围，绝不剧透结局。"
    if card:
        base += f"\n当前角色卡：{card['name']} - {card['system_prompt']}"
    return base


# ============== 命令 ==============
@bot.message_handler(commands=['start'])
def start(msg):
    user = get_or_create_user(msg.from_user.id, msg.from_user.username)
    bot.reply_to(msg,
                 f"✅ 欢迎来到纯DND剧本杀！赠送 **5000钻石** 💎\n当前DM等级：{user['ai_level']}\n\n指令：\n/level 1-5\n/gen 生成图片\n/recharge 卡密\n/savecard 创建角色卡")


@bot.message_handler(commands=['level'])
def set_level(msg):
    try:
        lvl = int(msg.text.split()[1])
        if 1 <= lvl <= 5:
            update_user(msg.from_user.id, ai_level=lvl)
            bot.reply_to(msg, f"✅ DM等级设置为 **{lvl}**")
        else:
            bot.reply_to(msg, "等级范围 1-5")
    except:
        bot.reply_to(msg, "用法：/level 3")


@bot.message_handler(commands=['gen'])
def gen_image(msg):
    user = get_or_create_user(msg.from_user.id, "")
    cost = user["ai_level"] * 12
    if not deduct_diamonds(msg.from_user.id, cost):
        return bot.reply_to(msg, "❌ 钻石不足！")

    prompt = msg.text.replace("/gen", "").strip() or "根据当前剧本生成主角或场景"
    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=f"高质量DND剧本杀风格图片：{prompt}，沉浸式氛围，细节丰富",
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"]
            )
        )
        image_bytes = None
        for part in response.parts:
            if part.inline_data:
                image_bytes = part.inline_data.data
                break
        if image_bytes:
            bot.send_photo(msg.chat.id, photo=io.BytesIO(image_bytes), caption="✅ Gemini 已生成剧本图片！")
        else:
            bot.reply_to(msg, "图片生成失败：未返回图像")
    except Exception as e:
        bot.reply_to(msg, f"图片生成失败：{str(e)[:150]}")


@bot.message_handler(commands=['recharge'])
def recharge(msg):
    try:
        code = msg.text.split(maxsplit=1)[1].strip()
        res = supabase.table("recharge_cards").select("*").eq("code", code).eq("used", False).execute()
        if res.data:
            d = res.data[0]["diamonds"]
            user = get_or_create_user(msg.from_user.id, "")
            update_user(msg.from_user.id, diamonds=user["diamonds"] + d)
            supabase.table("recharge_cards").update({"used": True}).eq("code", code).execute()
            bot.reply_to(msg, f"✅ 充值成功！+{d}钻石")
        else:
            bot.reply_to(msg, "❌ 卡密无效或已使用")
    except:
        bot.reply_to(msg, "用法：/recharge 卡密")


# ============== GM 命令（完整版） ==============
@bot.message_handler(commands=['gift'])
def gm_gift(msg):
    if msg.from_user.id != GM_ID:
        return
    try:
        tg_id = int(msg.text.split()[1])
        amount = int(msg.text.split()[2])
        user = get_or_create_user(tg_id, "")
        update_user(tg_id, diamonds=user["diamonds"] + amount)
        bot.reply_to(msg, f"✅ 已赠送 {amount} 钻石给 {tg_id}")
    except:
        bot.reply_to(msg, "用法：/gift <telegram_id> <钻石数>")


@bot.message_handler(commands=['addcard'])
def gm_addcard(msg):
    if msg.from_user.id != GM_ID:
        return
    bot.reply_to(msg, "GM专用：请使用 /savecard 功能让玩家自己创建")


# ============== 角色卡 ==============
@bot.message_handler(commands=['savecard'])
def save_card(msg):
    try:
        name = msg.text.split(maxsplit=1)[1]
        pending_cards[msg.from_user.id] = name
        bot.reply_to(msg, f"角色卡 **{name}** 已准备创建！请直接回复此消息输入角色设定")
    except:
        bot.reply_to(msg, "用法：/savecard 角色名")


@bot.message_handler(commands=['usecard'])
def use_card(msg):
    try:
        cid = int(msg.text.split()[1])
        res = supabase.table("role_cards").select("*").eq("id", cid).execute()
        if res.data:
            card = res.data[0]
            active_cards[msg.from_user.id] = card
            bot.reply_to(msg, f"✅ 已加载角色卡：{card['name']}")
        else:
            bot.reply_to(msg, "卡片ID不存在")
    except:
        bot.reply_to(msg, "用法：/usecard ID")


# ============== 主聊天（使用新 SDK + 历史对话） ==============
@bot.message_handler(func=lambda m: True)
def chat(msg):
    user_id = msg.from_user.id
    if user_id in pending_cards:
        name = pending_cards.pop(user_id)
        supabase.table("role_cards").insert({
            "owner_id": user_id,
            "name": name,
            "system_prompt": msg.text
        }).execute()
        bot.reply_to(msg, f"✅ 角色卡 **{name}** 保存成功！")
        return

    if msg.text.startswith("/"):
        return

    user = get_or_create_user(user_id, msg.from_user.username)
    cost = user["ai_level"] * 12
    if not deduct_diamonds(user_id, cost):
        bot.reply_to(msg, "❌ 钻石不足！")
        return

    system = get_system_prompt(user["ai_level"], active_cards.get(user_id))

    if user_id not in histories:
        histories[user_id] = client.chats.create(model=TEXT_MODEL)
        histories[user_id].send_message(system)   # 第一条消息设置为系统提示

    try:
        response = histories[user_id].send_message(msg.text)
        bot.reply_to(msg, response.text)
    except Exception as e:
        bot.reply_to(msg, f"DM出错：{str(e)[:150]}")


@bot.edited_message_handler(func=lambda m: True)
def edited(msg):
    user = get_or_create_user(msg.from_user.id, "")
    cost = user["ai_level"] * 12
    if deduct_diamonds(msg.from_user.id, cost):
        bot.reply_to(msg, "✅ 编辑消息已扣钻石，DM重新推进剧情")


# ============== 启动 ==============
if __name__ == "__main__":
    print("🚀 Gemini 纯DND剧本杀 Bot 已启动（Webhook 模式）")

    # 设置 Webhook
    bot.remove_webhook()
    webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
    bot.set_webhook(url=webhook_url)
    print(f"✅ Webhook 已设置为：{webhook_url}")

    # 启动 Flask
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
