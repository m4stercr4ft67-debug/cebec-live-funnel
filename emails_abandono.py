"""Sequência de recuperação (5 e-mails) para quem preencheu o formulário da live
e não concluiu a compra. Textos no tom da Angela: formal, emocional, inspirador.

Placeholders disponíveis em todos os templates (preenchidos no envio):
  {primeiro_nome}     primeiro nome do lead (fallback: "Olá")
  {checkout_url}      link de checkout (E1–E4: R$29; E5: R$19,90) já com UTMs
  {lp_url}            LP da live com UTMs
  {data_live}         ex.: "segunda-feira, 28 de setembro"
  {vagas_restantes}   vagas reais restantes (40 − compras da semana), mínimo 0
  {unsubscribe_url}   link de descadastro

Regra de conteúdo: nenhum número inventado. Depoimentos são reais (alunas CEBEC,
só primeiro nome). Escassez: 40 vagas por semana (real, confirmado pelo cliente).
"""

DEPOIMENTO_DIVISOR = (
    "“O CEBEC está sendo um divisor na minha carreira. Obrigada, Angela, por nos "
    "proporcionar tanto conhecimento.” — aluna CEBEC"
)
DEPOIMENTO_PATTY = (
    "“Além de nos capacitar, seu movimento nos possibilita nos conectarmos com "
    "pessoas incríveis, que vivem o mesmo propósito que a gente.” — Patty C., aluna CEBEC"
)
DEPOIMENTO_VANESSA = (
    "“Olha que eu já fiz muitas formações, mas o CEBEC está sendo construído com "
    "muito propósito.” — Vanessa N., aluna CEBEC"
)
DEPOIMENTO_ELIZIA = (
    "“Noite de muito aprendizado. Foi um encontro memorável. Gratidão por nos "
    "proporcionar horas de reflexões e muito conhecimento.” — Elizia S., participante"
)


SEQUENCIA = [
    {
        "step": 1,
        "subject": "{primeiro_nome}, sua vaga ainda está reservada",
        "preheader": "Faltou só um passo para você estar conosco na segunda às 19h.",
        "paragraphs": [
            "{primeiro_nome},",
            "Vi que você iniciou sua inscrição na live <strong>Gestão de Impacto na Prática</strong> e não chegou a concluir.",
            "Às vezes a vida nos interrompe no meio de uma decisão importante. Por isso guardei o seu lugar por mais algumas horas.",
            "Serão duas horas, ao vivo, sobre aquilo que transforma uma equipe por dentro: inteligência emocional, DISC, feedback assertivo e segurança psicológica. Com ferramentas para aplicar já na semana seguinte.",
        ],
        "cta": "Concluir minha inscrição",
        "after_cta": [
            "Nos vemos na {data_live}, às 19h.",
            "Com carinho,<br>Angela Pelizer",
        ],
    },
    {
        "step": 2,
        "subject": "O que você leva da live (e aplica na mesma semana)",
        "preheader": "Não é motivação. É metodologia com base em Gallup, Goleman, Rosenberg e Timothy Clark.",
        "paragraphs": [
            "{primeiro_nome},",
            "Quero ser muito transparente sobre o que acontece na segunda-feira, porque respeito o seu tempo.",
            "<strong>Você sai da live sabendo:</strong>",
            "<ul style='padding-left:20px;margin:0 0 16px'>"
            "<li style='margin-bottom:8px'>Por que o engajamento sobe para 73% quando a liderança foca nas forças das pessoas (dado Gallup) e como mapear essas forças na sua equipe;</li>"
            "<li style='margin-bottom:8px'>Como reconhecer os 4 perfis do DISC numa conversa comum, sem aplicar nenhum teste, e adaptar sua comunicação a cada um;</li>"
            "<li style='margin-bottom:8px'>Como dar um feedback que a pessoa consegue de fato ouvir, com os 4 passos da Comunicação Não Violenta;</li>"
            "<li style='margin-bottom:8px'>Como identificar em que estágio de segurança psicológica sua equipe está, e o que fazer para ela avançar.</li>"
            "</ul>",
            "Tudo isso vem da formação Gestão de Impacto, construída a partir de mais de 400 páginas de aulas e mentorias aplicadas em empresas brasileiras.",
            "E há algo que só o ao vivo oferece: você pode trazer a sua situação real e perguntar.",
        ],
        "cta": "Quero estar na live",
        "after_cta": [
            "São apenas 40 vagas por semana. Hoje restam {vagas_restantes}.",
            "Angela",
        ],
    },
    {
        "step": 3,
        "subject": "Crescer com desconforto ou adoecer em silêncio",
        "preheader": "Uma reflexão sobre o que nos impede de dar o próximo passo.",
        "paragraphs": [
            "{primeiro_nome},",
            "Há uma frase que repito muito às minhas alunas: <em>devemos crescer com desconforto ou adoecer em silêncio</em>.",
            "Quem cuida de pessoas costuma adiar o próprio desenvolvimento. Sempre há uma urgência maior, uma demanda de outra pessoa, um “depois eu vejo isso”.",
            "Talvez você esteja pensando que não tem tempo agora, ou que já conhece esses temas. Eu entendo. Mas conhecer não é o mesmo que saber aplicar diante de um conflito real, com a diretoria cobrando resultado e a equipe em silêncio.",
            "Veja o que me escreveram algumas pessoas que decidiram dar esse passo:",
            "<div style='border-left:3px solid #3D7A45;padding:4px 0 4px 16px;margin:0 0 12px;font-style:italic'>" + DEPOIMENTO_DIVISOR + "</div>"
            "<div style='border-left:3px solid #3D7A45;padding:4px 0 4px 16px;margin:0 0 12px;font-style:italic'>" + DEPOIMENTO_VANESSA + "</div>"
            "<div style='border-left:3px solid #3D7A45;padding:4px 0 4px 16px;margin:0 0 16px;font-style:italic'>" + DEPOIMENTO_PATTY + "</div>",
            "Amanhã, às 19h, eu estarei lá. Gostaria muito que você também estivesse.",
        ],
        "cta": "Garantir minha vaga",
        "after_cta": [
            "Restam {vagas_restantes} das 40 vagas desta semana.",
            "Com afeto,<br>Angela Pelizer",
        ],
    },
    {
        "step": 4,
        "subject": "É hoje, às 19h",
        "preheader": "{primeiro_nome}, as inscrições se encerram antes da live começar.",
        "paragraphs": [
            "{primeiro_nome},",
            "Hoje, às 19h, abrimos a sala da live <strong>Gestão de Impacto na Prática</strong>.",
            "Estou preparando cada detalhe para que você saia com ferramentas concretas, e não apenas com anotações bonitas.",
            "<div style='border-left:3px solid #3D7A45;padding:4px 0 4px 16px;margin:0 0 16px;font-style:italic'>" + DEPOIMENTO_ELIZIA + "</div>",
            "Você recebe acesso ao vivo e à gravação por 30 dias. Se algum imprevisto acontecer no horário, o conteúdo continua com você.",
        ],
        "cta": "Garantir minha vaga para hoje",
        "after_cta": [
            "Restam {vagas_restantes} vagas. As inscrições encerram às 18h30.",
            "Angela",
        ],
    },
    {
        "step": 5,
        "subject": "Uma condição que eu faço uma única vez",
        "preheader": "Até as 18h30 de hoje: sua vaga por R$19,90.",
        "paragraphs": [
            "{primeiro_nome},",
            "Este é o último e-mail que envio sobre a live de hoje.",
            "Eu sei que cada pessoa tem o seu momento, e não quero que o valor seja o motivo de você ficar de fora de algo que pode mudar a forma como você cuida da sua equipe.",
            "Por isso, <strong>somente por este link e somente até as 18h30 de hoje</strong>, sua vaga sai por <strong>R$19,90</strong> em vez de R$29.",
            "Mesma live, mesmo acesso ao vivo, mesma gravação por 30 dias.",
        ],
        "cta": "Garantir minha vaga por R$19,90",
        "after_cta": [
            "Depois das 18h30 este link deixa de funcionar e as inscrições se encerram.",
            "Espero ver você lá dentro.<br>Angela Pelizer",
        ],
    },
]
