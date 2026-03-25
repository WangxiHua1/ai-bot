import telebot
import os
import json
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from dotenv import load_dotenv
from openai import OpenAI
from telebot.types import InputChecklist, InputChecklistTask

load_dotenv()

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
GM_ID = int(os.getenv("GM_ID") or 0)
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")

bot = telebot.TeleBot(BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==================== xAI Grok 配置 ====================
client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)
MODEL = "grok-4.20-reasoning"   # 剧情最强模型

user_cache = {}
active_cards = {}
active_tasks = {}   # 每个用户动态任务列表（实时更新）

# ============== Flask + CORS ==============
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.route('/')
def home():
    return "DND剧本杀 Bot (xAI Grok + 动态 Checklist) 正常运行！UptimeRobot 已监测到 UP"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'ERROR', 400

# ============== AI回复格式化（动作斜体 + 对话粗体） ==============
def format_ai_reply(text: str):
    lines = text.split('\n')
    formatted = []
    for line in lines:
        line = line.strip()
        if any(k in line.lower() for k in ["叙事", "动作", "描述", "场景", "环境", "剧情"]):
            formatted.append(f"<i>{line}</i>")
        elif any(k in line.lower() for k in ["对话", "说", "：", '"']) or '"' in line:
            formatted.append(f"<b>{line}</b>")
        else:
            formatted.append(line)
    return "\n".join(formatted)

# ============== 解析 AI 回复中的新任务 ==============
def extract_new_tasks(text: str):
    match = re.search(r'\*\*新任务：\*\*\s*(\[.*?\])', text, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            tasks = json.loads(match.group(1))
            if isinstance(tasks, list):
                return [str(t).strip() for t in tasks if str(t).strip()]
        except:
            pass
    return []

# ============== 发送/更新 Checklist ==============
def send_updated_checklist(chat_id: int, tg_id: int):
    tasks_list = active_tasks.get(tg_id, [
        "调查废弃古堡的秘密",
        "找到失落的精灵之戒",
        "解开古老的诅咒"
    ])
    if not tasks_list:
        return
    checklist_tasks = [InputChecklistTask(id=i+1, text=task) for i, task in enumerate(tasks_list)]
    checklist = InputChecklist(
        title="📋 本场 DND 剧本杀主线任务（实时更新）",
        tasks=checklist_tasks,
        others_can_mark_tasks_as_done=True
    )
    bot.send_message(
        chat_id,
        "🎉 任务已实时更新！点击方框勾选，完成任务我会继续推进剧情～",
        reply_markup=checklist
    )

# ============== Streaming 流式回复 + 实时动态任务 ==============
def stream_reply(chat_id: int, tg_id: int, user_message: str, system_prompt: str):
    bot.send_chat_action(chat_id, 'typing')
    msg = bot.send_message(chat_id, "▌", parse_mode='HTML')
    
    full_text = ""
    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.85,
            stream=True
        )
        for chunk in stream:
            if chunk.choices[0].delta.content:
                full_text += chunk.choices[0].delta.content
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=format_ai_reply(full_text) + "▌",
                        parse_mode='HTML'
                    )
                except:
                    pass
    except Exception as e:
        bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="AI生成出错，请重试", parse_mode='HTML')
        return
    
    # 最终回复
    final_reply = format_ai_reply(full_text)
    bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=final_reply, parse_mode='HTML')
    
    # 实时动态添加任务
    new_tasks = extract_new_tasks(full_text)
    if new_tasks:
        if tg_id not in active_tasks:
            active_tasks[tg_id] = []
        active_tasks[tg_id].extend(new_tasks)
        active_tasks[tg_id] = list(dict.fromkeys(active_tasks[tg_id]))  # 去重
        send_updated_checklist(chat_id, tg_id)

# ============== Checklist 命令 ==============
@bot.message_handler(commands=['tasks'])
def send_checklist(msg):
    send_updated_checklist(msg.chat.id, msg.from_user.id)

# ============== 监听任务勾选 ==============
@bot.message_handler(content_types=['checklist_task_done'])
def handle_checklist_done(msg):
    bot.send_message(msg.chat.id, f"✅ 任务已完成！DM正在根据你的进度继续推进剧情…")

# ============== 系统 Prompt（强制输出格式 + 新任务） ==============
def get_system_prompt(level: int, card=None):
    base = f"""你是专业的DND剧本杀 Dungeon Master（DM），等级{level}。
用第二人称沉浸式叙事，推进剧情，描述NPC、场景、感官细节。
**严格按以下格式回复**（不要加任何额外说明）：

**叙事/动作：** 这里写所有环境、动作、感官描述
**对话：** "NPC说的每一句话"

在回复**最后**必须加上：
**新任务：** ["新任务1", "新任务2"] （没有新任务就写 []）

保持角色一致性，剧情连贯。"""
    if card:
        base += f"\n当前角色卡：{card.get('name', '未知')} {card.get('system_prompt', '')}"
    return base

# ============== 用户系统（完整保留） ==============
def get_or_create_user(tg_id: int, username: str):
    try:
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
                "ai_level": 1,
                "mode": "normal"
            }
            supabase.table("users").insert(user).execute()
            user = supabase.table("users").select("*").eq("telegram_id", tg_id).execute().data[0]
        user_cache[tg_id] = user
        return user
    except Exception as e:
        print(f"用户创建出错: {e}")
        return {"telegram_id": tg_id, "username": username or "unknown", "diamonds": 5000, "ai_level": 1, "mode": "normal"}

def update_user(tg_id, **kwargs):
    try:
        if tg_id in user_cache:
            user_cache[tg_id].update(kwargs)
        supabase.table("users").update(kwargs).eq("telegram_id", tg_id).execute()
    except Exception as e:
        print(f"更新用户出错: {e}")

def deduct_diamonds(tg_id: int, cost: int) -> bool:
    user = get_or_create_user(tg_id, "")
    if user["diamonds"] < cost:
        return False
    update_user(tg_id, diamonds=user["diamonds"] - cost)
    return True

# ============== 所有原有命令（完整保留） ==============
@bot.message_handler(commands=['ping'])
def ping(msg):
    bot.reply_to(msg, "Webhook 正常！Bot 已收到消息！\n\n现在试试 /start")

@bot.message_handler(commands=['start'])
def start(msg):
    user = get_or_create_user(msg.from_user.id, msg.from_user.username)
    bot.reply_to(msg, f"欢迎来到纯DND剧本杀！赠送 **5000钻石** 💎\n当前DM等级：{user['ai_level']}\n\n指令：\n/level 1-5\n/gen 生成图片\n/recharge 卡密\n/savecard 创建角色卡\n/usecard ID\n/myid 查看你的ID\n/tasks 查看最新任务清单\n/ping 测试")

@bot.message_handler(commands=['myid'])
def my_id(msg):
    bot.reply_to(msg, f"你的 Telegram ID 是：\n**{msg.from_user.id}**")

@bot.message_handler(commands=['level'])
def set_level(msg):
    try:
        lvl = int(msg.text.split()[1])
        if 1 <= lvl <= 5:
            update_user(msg.from_user.id, ai_level=lvl)
            bot.reply_to(msg, f"DM等级设置为 **{lvl}**")
        else:
            bot.reply_to(msg, "等级范围 1-5")
    except:
        bot.reply_to(msg, "用法：/level 3")

@bot.message_handler(commands=['gen'])
def gen_image(msg):
    bot.reply_to(msg, "❌ xAI 当前暂无图像生成支持\n请使用文字剧情模式，或等待官方更新")

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
            bot.reply_to(msg, f"充值成功！+{d}钻石")
        else:
            bot.reply_to(msg, "卡密无效或已使用")
    except:
        bot.reply_to(msg, "用法：/recharge 卡密")

@bot.message_handler(commands=['gift'])
def gm_gift(msg):
    if msg.from_user.id != GM_ID: return
    try:
        tg_id = int(msg.text.split()[1])
        amount = int(msg.text.split()[2])
        user = get_or_create_user(tg_id, "")
        update_user(tg_id, diamonds=user["diamonds"] + amount)
        bot.reply_to(msg, f"已赠送 {amount} 钻石给 {tg_id}")
    except:
        bot.reply_to(msg, "用法：/gift <telegram_id> <钻石数>")

# ============== AI 主聊天（动态 Checklist 已启用） ==============
@bot.message_handler(func=lambda message: True)
def handle_ai_chat(msg):
    if msg.text.startswith('/'): return
    
    tg_id = msg.from_user.id
    user = get_or_create_user(tg_id, msg.from_user.username)
    cost = user.get("ai_level", 1) * 8
    if not deduct_diamonds(tg_id, cost):
        return bot.reply_to(msg, "❌ 钻石不足！请 /recharge")
    
    card = active_cards.get(tg_id)
    system_prompt = get_system_prompt(user["ai_level"], card)
    
    bot.send_message(msg.chat.id, f"💎 已扣除 {cost} 钻石（剩余 {user.get('diamonds', 0) - cost}）")
    stream_reply(msg.chat.id, tg_id, msg.text, system_prompt)

# ============== /api/chat（供 Vercel WebApp 使用） ==============
@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json or {}
    tg_id = data.get('tg_id') or data.get('user_id')
    card = data.get('card')
    message = data.get('message')

    if not all([tg_id, card, message]):
        return jsonify({"error": "缺少参数"}), 400

    user = get_or_create_user(tg_id, "")
    cost = user.get("ai_level", 1) * 8
    if not deduct_diamonds(tg_id, cost):
        return jsonify({"error": "钻石不足", "cost": cost}), 402

    system_prompt = get_system_prompt(user["ai_level"], card)
    
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            temperature=0.9
        )
        ai_reply = response.choices[0].message.content
        formatted = format_ai_reply(ai_reply)
        return jsonify({"reply": formatted, "cost": cost})
    except Exception as e:
        print(f"xAI 错误: {e}")
        return jsonify({"error": "AI生成失败"}), 500

# ============== 启动 ==============
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
