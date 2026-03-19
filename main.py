import telebot
import os
import google.generativeai as genai
from supabase import create_client
from dotenv import load_dotenv
import io
from flask import Flask
import threading

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GM_ID = int(os.getenv("GM_ID", 0))

bot = telebot.TeleBot(BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

genai.configure(api_key=GEMINI_API_KEY)
text_model = genai.GenerativeModel('gemini-2.5-flash')
image_model = genai.GenerativeModel('gemini-2.5-flash-image')

user_cache = {}
histories = {}
active_cards = {}
pending_cards = {}  # 用于保存角色卡

def get_or_create_user(tg_id: int, username: str):
    if tg_id in user_cache: return user_cache[tg_id]
    res = supabase.table("users").select("*").eq("telegram_id", tg_id).execute()
    if res.data:
        user = res.data[0]
    else:
        user = {"telegram_id": tg_id, "username": username, "diamonds": 5000, "ai_level": 1, "mode": "dnd"}
        supabase.table("users").insert(user).execute()
    user_cache[tg_id] = user
    return user

def update_user(tg_id, **kwargs):
    if tg_id in user_cache: user_cache[tg_id].update(kwargs)
    supabase.table("users").update(kwargs).eq("telegram_id", tg_id).execute()

def deduct_diamonds(tg_id: int, cost: int) -> bool:
    user = get_or_create_user(tg_id, "")
    if user["diamonds"] < cost: return False
    update_user(tg_id, diamonds=user["diamonds"] - cost)
    return True

def get_system_prompt(level: int, card=None):
    base = f"你是专业的DND剧本杀 Dungeon Master（DM），等级{level}（越高剧情越复杂、细节越丰富、推理越智能）。\n用第二人称沉浸式叙事，推进主线剧情，描述NPC对话、场景氛围、感官细节、线索和悬疑转折。\n玩家是主角，保持公平、紧张刺激的剧本杀氛围，绝不剧透结局。"
    if card: base += f"\n当前角色卡：{card['name']} - {card['system_prompt']}"
    return base

# ==================== 命令 ====================
@bot.message_handler(commands=['start'])
def start(msg):
    user = get_or_create_user(msg.from_user.id, msg.from_user.username)
    bot.reply_to(msg, f"✅ 欢迎来到纯DND剧本杀！赠送 **5000钻石** 💎\n当前DM等级：{user['ai_level']}\n\n指令菜单：\n/level 1-5\n/gen 生成图片\n/recharge 卡密\n/savecard 创建角色卡\n/usecard ID 使用角色卡")

@bot.message_handler(commands=['level'])
def set_level(msg):
    try:
        lvl = int(msg.text.split()[1])
        if 1 <= lvl <= 5:
            update_user(msg.from_user.id, ai_level=lvl)
            bot.reply_to(msg, f"✅ DM等级设置为 **{lvl}**（越高剧情越精彩）")
    except:
        bot.reply_to(msg, "用法：/level 3")

@bot.message_handler(commands=['gen'])
def gen_image(msg):
    user = get_or_create_user(msg.from_user.id, "")
    if not deduct_diamonds(msg.from_user.id, user["ai_level"] * 12):
        return bot.reply_to(msg, "❌ 钻石不足！")
    prompt = msg.text.replace("/gen", "").strip() or "根据当前剧本生成主角或场景"
    try:
        response = image_model.generate_content(f"高质量DND剧本杀风格图片：{prompt}，沉浸式氛围，细节丰富，无任何NSFW")
        image_bytes = response.parts[0].inline_data.data
        bot.send_photo(msg.chat.id, photo=io.BytesIO(image_bytes), caption="✅ Gemini 2.5 Flash Image 已生成剧本图片！")
    except Exception as e:
        bot.reply_to(msg, f"图片生成失败（每日500张免费限额）：{str(e)[:100]}")

@bot.message_handler(commands=['recharge'])
def recharge(msg):
    try:
        code = msg.text.split()[1]
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

# GM权限
@bot.message_handler(commands=['gift', 'addcard'])
def gm_commands(msg):
    if msg.from_user.id != GM_ID: 
        bot.reply_to(msg, "无GM权限")
        return
    if msg.text.startswith('/gift'):
        try:
            _, uid, amt = msg.text.split()
            uid = int(uid.replace("@", ""))
            user = get_or_create_user(uid, "")
            update_user(uid, diamonds=user["diamonds"] + int(amt))
            bot.reply_to(msg, f"GM已赠送 {amt}钻石给用户 {uid}")
        except:
            bot.reply_to(msg, "用法：/gift 用户ID 数量")
    elif msg.text.startswith('/addcard'):
        try:
            _, code, diamonds = msg.text.split()
            supabase.table("recharge_cards").insert({"code": code, "diamonds": int(diamonds)}).execute()
            bot.reply_to(msg, f"✅ 卡密 {code}（{diamonds}钻石）创建成功")
        except:
            bot.reply_to(msg, "用法：/addcard 卡密 钻石数")

# 角色卡系统
@bot.message_handler(commands=['savecard'])
def save_card(msg):
    try:
        name = msg.text.split(maxsplit=1)[1]
        pending_cards[msg.from_user.id] = name
        bot.reply_to(msg, f"角色卡 **{name}** 已准备创建！\n请**直接回复此消息**输入角色系统提示词（性格、背景、能力等，越详细越好）")
    except:
        bot.reply_to(msg, "用法：/savecard 角色名（例如 /savecard 精灵游侠）")

@bot.message_handler(commands=['usecard'])
def use_card(msg):
    try:
        cid = int(msg.text.split()[1])
        card = supabase.table("role_cards").select("*").eq("id", cid).execute().data[0]
        active_cards[msg.from_user.id] = card
        bot.reply_to(msg, f"✅ 已加载角色卡：{card['name']}（剧情将使用此设定）")
    except:
        bot.reply_to(msg, "用法：/usecard 卡片ID（先用 /savecard 创建）")

# ==================== 主聊天（纯DND + 角色卡保存） ====================
@bot.message_handler(func=lambda m: True)
def chat(msg):
    user_id = msg.from_user.id
    if msg.text.startswith("/"): return

    # 处理 pending 角色卡保存
    if user_id in pending_cards:
        name = pending_cards.pop(user_id)
        supabase.table("role_cards").insert({
            "owner_id": user_id,
            "name": name,
            "system_prompt": msg.text,
            "is_public": False
        }).execute()
        bot.reply_to(msg, f"✅ 角色卡 **{name}** 保存成功！用 /usecard ID 加载")
        return

    user = get_or_create_user(user_id, msg.from_user.username)
    cost = user["ai_level"] * 12
    if not deduct_diamonds(user_id, cost):
        return bot.reply_to(msg, "❌ 钻石不足！")

    if user_id not in histories:
        system = get_system_prompt(user["ai_level"], active_cards.get(user_id))
        histories[user_id] = text_model.start_chat(history=[{"role": "user", "parts": [system]}])

    try:
        response = histories[user_id].send_message(msg.text)
        reply = response.text
        bot.reply_to(msg, reply)
    except Exception as e:
        bot.reply_to(msg, f"DM剧情出错：{str(e)[:100]}")

@bot.edited_message_handler(func=lambda m: True)
def edited(msg):
    if deduct_diamonds(msg.from_user.id, get_or_create_user(msg.from_user.id, "")["ai_level"] * 12):
        bot.reply_to(msg, "✅ 编辑消息已扣钻石，DM重新推进剧情")
        
def run_bot():
    print("🚀 Gemini 纯DND剧本杀 Bot 已启动")
    bot.infinity_polling()

# ============== Flask 健康检查（Railway 必须有这个才能显示 UP） ==============
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ DND剧本杀 Bot 正常运行！UptimeRobot 已监测到 UP"

if __name__ == "__main__":
    print("🚀 Gemini 纯DND剧本杀 Bot 已启动")
    print("🔧 Bot 线程准备启动...")

    def run_polling():
        try:
            print("📡 Bot polling 已启动！现在可以发 /start 测试了")
            bot.infinity_polling(
                none_stop=True,
                interval=0,
                timeout=20,
                drop_pending_updates=True   # ← 关键：丢弃旧冲突消息
            )
        except Exception as e:
            print(f"❌ Polling 崩溃: {e}")

    bot_thread = threading.Thread(target=run_polling)
    bot_thread.daemon = True
    bot_thread.start()

    # Flask 健康检查
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
