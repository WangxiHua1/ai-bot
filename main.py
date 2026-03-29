import telebot
import os
import json
import re
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
GM_ID = int(os.getenv("GM_ID", "0"))  # DM 管理员 Telegram ID
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "your-railway-app.up.railway.app")

bot = telebot.TeleBot(BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==================== xAI Grok 配置 ====================
client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

# 支持的模型（文本 + 图片）
TEXT_MODEL = "grok-4.20-reasoning"      # 文本最强
IMAGE_MODEL = "grok-2-image"            # xAI 图片生成模型（Flux 驱动）

# 缓存
user_cache = {}
chat_histories = {}      # {user_id: [{"role": "user/ai", "text": "...", "level": int}, ...]}
active_character = {}    # {user_id: character_id} 当前正在聊天的角色
generated_images = []    # 全局图片画廊（后续可存 Supabase）

# ============== Flask + Webhook（Railway 部署） ==============
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.route('/')
def home():
    return "✅ AI角色 Bot (xAI Grok + 人物卡系统) 正常运行！UptimeRobot 已监测 UP"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'ERROR', 400

# ============== 工具函数 ==============
def get_or_create_user(tg_id: int, username: str):
    if tg_id in user_cache:
        return user_cache[tg_id]
    res = supabase.table("users").select("*").eq("telegram_id", tg_id).execute()
    if res.data:
        user = res.data[0]
    else:
        user = {
            "telegram_id": tg_id,
            "username": username or "unknown",
            "diamonds": 5000,
            "ai_level": 3
        }
        supabase.table("users").insert(user).execute()
        user = supabase.table("users").select("*").eq("telegram_id", tg_id).execute().data[0]
    user_cache[tg_id] = user
    return user

def update_user(tg_id: int, **kwargs):
    if tg_id in user_cache:
        user_cache[tg_id].update(kwargs)
    supabase.table("users").update(kwargs).eq("telegram_id", tg_id).execute()

def deduct_diamonds(tg_id: int, amount: int) -> bool:
    user = get_or_create_user(tg_id, "")
    if user["diamonds"] >= amount:
        update_user(tg_id, diamonds=user["diamonds"] - amount)
        return True
    bot.send_message(tg_id, "💎 钻石不足！请使用 /recharge 充值")
    return False

# ============== 人物卡数据库操作 ==============
def get_all_characters():
    res = supabase.table("character_cards").select("*").execute()
    return res.data or []

def get_my_characters(tg_id: int):
    res = supabase.table("character_cards").select("*").eq("owner_id", tg_id).execute()
    return res.data or []

def get_collected_characters(tg_id: int):
    res = supabase.table("collections").select("*, character_cards(*)").eq("user_id", tg_id).execute()
    return [item["character_cards"] for item in res.data] if res.data else []

def create_character(tg_id: int, data: dict):
    character = {
        "owner_id": tg_id,
        "name": data["name"],
        "type": data["type"],           # 养成 / 剧本
        "nsfw": data.get("nsfw", False),
        "fetish": data.get("fetish", ""),
        "tags": data.get("tags", []),
        "description": data["description"],
        "favor": 10 if data["type"] == "养成" else 0,
        "body_dev": 0,
        "is_public": data.get("is_public", True)
    }
    res = supabase.table("character_cards").insert(character).execute()
    return res.data[0]

# ============== AI 回复生成（根据等级智能度不同） ==============
def get_system_prompt(level: int, char: dict):
    base = f"""你是一个极具沉浸感的 AI 角色扮演助手，当前智能等级 {level}（1-5，越高越聪明、回复越长、越有创意）。
角色信息：{char['name']} - {char['description']}
类型：{char['type']}系统 {"(NSFW 已开启)" if char['nsfw'] else ""}
性癖：{char.get('fetish', '无')}

回复要求：
1. 严格区分【动作/描述】（斜体） 和 【对话】（加粗）
2. 根据等级调整：
   - 等级1：短回复
   - 等级3：中等长度
   - 等级5：超长、极度细节、情感丰富
3. 如果是养成系统，自动更新好感度并在回复末尾写日记
4. 如果 NSFW，根据对话增加身体开发度
5. 最后一行必须是 JSON：{{"favor": X, "body_dev": Y, "diary": "..."}} （仅当养成/NSFW 时）
"""
    return base

def stream_ai_reply(chat_id: int, tg_id: int, user_message: str, char: dict):
    user = get_or_create_user(tg_id, "")
    level = user["ai_level"]
    cost = level * 80
    if not deduct_diamonds(tg_id, cost):
        return

    bot.send_chat_action(chat_id, 'typing')
    msg = bot.send_message(chat_id, "▌ 思考中...", parse_mode='HTML')

    full_text = ""
    try:
        stream = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": get_system_prompt(level, char)},
                {"role": "user", "content": user_message}
            ],
            temperature=0.9,
            stream=True
        )
        for chunk in stream:
            if chunk.choices[0].delta.content:
                full_text += chunk.choices[0].delta.content
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=full_text + "▌",
                        parse_mode='HTML'
                    )
                except:
                    pass
    except Exception as e:
        bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="❌ xAI 生成失败，请重试")
        return

    # 最终回复
    bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=full_text, parse_mode='HTML')

    # 解析最后 JSON 更新状态
    try:
        json_match = re.search(r'\{.*\}', full_text, re.DOTALL)
        if json_match:
            stats = json.loads(json_match.group(0))
            # 更新人物卡
            update_data = {}
            if char["type"] == "养成":
                update_data["favor"] = stats.get("favor", char["favor"])
            if char["nsfw"]:
                update_data["body_dev"] = stats.get("body_dev", char["body_dev"])
            if update_data:
                supabase.table("character_cards").update(update_data).eq("id", char["id"]).execute()
    except:
        pass

    # 保存聊天记录
    if tg_id not in chat_histories:
        chat_histories[tg_id] = []
    chat_histories[tg_id].append({"role": "user", "text": user_message, "time": datetime.now().strftime("%H:%M")})
    chat_histories[tg_id].append({"role": "ai", "text": full_text, "time": datetime.now().strftime("%H:%M")})

# ============== 图片生成（xAI） ==============
def generate_image(chat_id: int, prompt: str, char_name: str):
    bot.send_chat_action(chat_id, 'upload_photo')
    try:
        response = client.images.generate(
            model=IMAGE_MODEL,
            prompt=f"{prompt}，角色：{char_name}，风格：动漫，高质量，细节丰富",
            n=1,
            size="1024x1024"
        )
        image_url = response.data[0].url
        # 保存到全局画廊
        generated_images.append({
            "url": image_url,
            "desc": f"{char_name} - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "time": datetime.now().isoformat()
        })
        bot.send_photo(chat_id, image_url, caption=f"✅ xAI 生成成功！已保存到画廊")
        return image_url
    except Exception as e:
        bot.send_message(chat_id, f"图片生成失败：{str(e)}")
        return None

# ============== Telegram 命令 & 消息处理 ==============
@bot.message_handler(commands=['start'])
def start(msg):
    bot.send_message(msg.chat.id, "👋 欢迎来到 AI角色！\n\n"
                                  "发送 /create 创建人物卡\n"
                                  "发送 /list 查看所有角色\n"
                                  "回复任意消息即可和当前角色聊天\n"
                                  "DM 管理命令仅限管理员使用")

@bot.message_handler(commands=['level'])
def set_level(msg):
    try:
        level = int(msg.text.split()[1])
        if 1 <= level <= 5:
            update_user(msg.from_user.id, ai_level=level)
            bot.send_message(msg.chat.id, f"✅ AI 智能等级已切换为 {level} 级\n更高等级回复更智能，但消耗更多钻石")
        else:
            bot.send_message(msg.chat.id, "等级只能是 1-5")
    except:
        bot.send_message(msg.chat.id, "用法：/level 3")

@bot.message_handler(commands=['balance'])
def show_balance(msg):
    user = get_or_create_user(msg.from_user.id, msg.from_user.username)
    bot.send_message(msg.chat.id, f"💎 你的钻石余额：{user['diamonds']}")

@bot.message_handler(commands=['recharge'])
def recharge(msg):
    bot.send_message(msg.chat.id, "💳 请输入卡密（格式：/recharge 卡密内容）\n1 RMB = 1000 钻石")

@bot.message_handler(commands=['recharge'])
def handle_recharge(msg):
    try:
        key = msg.text.split(maxsplit=1)[1].strip()
        # 模拟卡密验证（实际可存 Supabase 卡密表）
        if len(key) > 5:  # 简单验证
            diamonds_add = 5000
            user = get_or_create_user(msg.from_user.id, "")
            update_user(msg.from_user.id, diamonds=user["diamonds"] + diamonds_add)
            bot.send_message(msg.chat.id, f"✅ 充值成功！+{diamonds_add} 钻石")
        else:
            bot.send_message(msg.chat.id, "❌ 卡密无效")
    except:
        bot.send_message(msg.chat.id, "用法：/recharge 你的卡密")

@bot.message_handler(commands=['create'])
def create_cmd(msg):
    bot.send_message(msg.chat.id, "请使用以下格式创建人物卡（一行一条）\n"
                                  "名称: XXX\n"
                                  "类型: 养成/剧本\n"
                                  "NSFW: 是/否\n"
                                  "标签: 温柔,校园\n"
                                  "描述: ...")
    # 实际生产中建议使用 conversation 状态机，这里简化

# ============== 人物卡列表 & 进入聊天 ==============
@bot.message_handler(commands=['list'])
def list_characters(msg):
    chars = get_all_characters()
    text = "📋 所有人物卡：\n\n"
    for c in chars:
        nsfw_tag = "🔞NSFW " if c["nsfw"] else ""
        text += f"{c['name']} {nsfw_tag}({c['type']}) - {c['description'][:30]}...\n"
    bot.send_message(msg.chat.id, text + "\n回复角色名称即可开始聊天")

@bot.message_handler(func=lambda m: True)
def handle_all_messages(msg):
    tg_id = msg.from_user.id
    text = msg.text.strip()

    # DM 隐藏管理功能（仅管理员）
    if tg_id == GM_ID:
        if text.startswith('/gift'):
            try:
                _, target_id, amount = text.split()
                target_id = int(target_id)
                amount = int(amount)
                target_user = get_or_create_user(target_id, "")
                update_user(target_id, diamonds=target_user["diamonds"] + amount)
                bot.send_message(tg_id, f"已赠送 {amount} 钻石给 {target_id}")
                bot.send_message(target_id, f"🎁 管理员赠送你 {amount} 钻石！")
            except:
                pass
        # 其他 DM 操作类似...

    # 如果用户当前有活跃角色，则进入对话
    if tg_id in active_character:
        char_id = active_character[tg_id]
        char = next((c for c in get_all_characters() if c["id"] == char_id), None)
        if char:
            stream_ai_reply(msg.chat.id, tg_id, text, char)
            return

    # 否则尝试匹配角色名称开始聊天
    chars = get_all_characters()
    for c in chars:
        if c["name"] in text:
            active_character[tg_id] = c["id"]
            bot.send_message(msg.chat.id, f"✅ 已进入与 {c['name']} 的聊天！\n直接发消息即可对话")
            # 可选：发送欢迎图片
            return

    bot.send_message(msg.chat.id, "没有找到匹配角色，请先 /list 查看或 /create 创建")

# ============== 图片生成命令 ==============
@bot.message_handler(commands=['img'])
def gen_img(msg):
    if tg_id := msg.from_user.id not in active_character:
        bot.send_message(msg.chat.id, "请先进入一个角色聊天再生成图片")
        return
    char_id = active_character[tg_id]
    char = next((c for c in get_all_characters() if c["id"] == char_id), None)
    prompt = msg.text.replace("/img", "").strip() or "根据对话生成一张角色图片"
    generate_image(msg.chat.id, prompt, char["name"])

# ============== 画廊查看 ==============
@bot.message_handler(commands=['gallery'])
def show_gallery(msg):
    if not generated_images:
        bot.send_message(msg.chat.id, "画廊暂无图片")
        return
    for img in generated_images[-5:]:  # 最近5张
        bot.send_photo(msg.chat.id, img["url"], caption=img["desc"])
    
# ====================== 前端 API 接口（新增） ======================
@app.route('/api/characters', methods=['GET'])
def api_characters():
    # 返回所有公开的人物卡（探索页）
    chars = get_all_characters()
    return jsonify([c for c in chars if c.get("is_public", True)])

@app.route('/api/my-characters', methods=['GET'])
def api_my_characters():
    tg_id = request.args.get('tg_id')
    if not tg_id:
        return jsonify({"error": "缺少 tg_id"}), 400
    return jsonify(get_my_characters(int(tg_id)))

@app.route('/api/create-character', methods=['POST'])
def api_create_character():
    data = request.json
    tg_id = data.get('tg_id')
    if not tg_id:
        return jsonify({"error": "缺少 tg_id"}), 400
    char = create_character(int(tg_id), data)
    return jsonify(char), 201

@app.route('/api/send-message', methods=['POST'])
def api_send_message():
    data = request.json
    tg_id = data.get('tg_id')
    char_id = data.get('char_id')
    message = data.get('message')
    if not all([tg_id, char_id, message]):
        return jsonify({"error": "参数不完整"}), 400
    
    char = next((c for c in get_all_characters() if c["id"] == char_id), None)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    
    # 调用 xAI 生成回复
    reply = stream_ai_reply(None, int(tg_id), message, char)  # 这里复用你原来的逻辑
    return jsonify({"reply": reply})

@app.route('/api/generate-image', methods=['POST'])
def api_generate_image():
    data = request.json
    char_id = data.get('char_id')
    prompt = data.get('prompt', '')
    char = next((c for c in get_all_characters() if c["id"] == char_id), None)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    url = generate_image(None, prompt, char["name"])
    return jsonify({"image_url": url})

@app.route('/api/balance', methods=['GET'])
def api_balance():
    tg_id = request.args.get('tg_id')
    if not tg_id:
        return jsonify({"error": "缺少 tg_id"}), 400
    user = get_or_create_user(int(tg_id), "")
    return jsonify({"diamonds": user["diamonds"], "ai_level": user["ai_level"]})

@app.route('/api/gallery', methods=['GET'])
def api_gallery():
    return jsonify(generated_images[-10:])  # 最近10张

# ============== 启动 ==============
if __name__ == "__main__":
    # 设置 webhook（Railway 部署）
    if RAILWAY_PUBLIC_DOMAIN:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook")
        print("✅ Webhook 已设置")
    else:
        print("⚠️ 本地运行，使用 polling")
        bot.infinity_polling()
