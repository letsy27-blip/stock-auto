from ai.ai_chat import ask_stock_ai

while True:
    q = input("질문: ")

    if q.lower() in ["q", "quit", "exit"]:
        break

    answer = ask_stock_ai(q)
    print("\n답변:")
    print(answer)
    print("-" * 60)