"""The closed category set.

Videos can only be filed under these names, and migrations/001_init.sql enforces
the same list with a CHECK constraint. Each "name: definition" is embedded by the
local model (classify.py) and compared with the video's text, so the wording
directly shapes classification. Edit the definitions to match what each old
folder actually meant to you, then re-run with --reprocess.

Renaming or adding a category needs a new migration that updates the
videos_categories_check constraint as well as this file.
"""

CATEGORIES: dict[str, str] = {
    "Good Habits": "Routines, discipline, health and productivity habits worth adopting.",
    "Job Search": "Finding and landing a job: resumes, applications, job boards, recruiters, interviews.",
    "Information": "General knowledge, facts and explainers that don't fit a more specific category.",
    "Motivation": "Inspirational or mindset content meant to push you to act.",
    "Thoughts": "Reflections, opinions and life philosophy worth thinking about.",
    "Apps & Tools": "Specific apps, websites, extensions and software tools worth trying.",
    "Food": "Recipes, cooking techniques, restaurants and food culture.",
    "Movies & Series": "Film and TV recommendations, reviews and trivia.",
    "Life Hacks": "Practical everyday tricks for home, money, travel and organisation.",
    "Social Skills": "Communication, conversation, charisma, relationships and body language.",
    "Coding": "Programming and software engineering: languages, frameworks, tools, practices.",
    "AI": "Artificial intelligence, LLMs, AI tools, prompting and AI news.",
    "Music": "Songs, artists, recommendations, music theory and production (non-piano).",
    "Ideas": "Ideas and inspiration to explore later: creative, content, gift or business ideas.",
    "Side Hustles": "Ways to earn extra income: freelancing, small businesses, online income.",
    "Acquire": "Specific products or things to buy or get; wishlist material.",
    "Reading": "Book recommendations, book summaries and reading habits.",
    "3D": "3D modelling, Blender, CAD, 3D printing and 3D art or animation.",
    "Job Tips": "Doing well once employed: workplace skills, career growth, salary negotiation.",
    "To Talk": "Stories or topics worth bringing up in conversation with other people.",
    "To Question": "Surprising or dubious claims that should be fact-checked before believing.",
    "Videogames": "Video games, gaming news, recommendations and game design.",
    "Languages": "Learning foreign languages: vocabulary, grammar, pronunciation, methods.",
    "Break into Tech": "Getting into the tech industry: career switches, portfolios, bootcamps, first tech job.",
    "Around the World": "Travel, places, countries, cultures and geography.",
    "Piano": "Playing piano: tutorials, pieces, technique and practice.",
    "Gadgets": "Physical tech devices and accessories: showcases, reviews, cool hardware.",
    "Technology": "Tech news and how technology works, beyond coding and AI specifically.",
    "Remote Jobs": "Remote work opportunities, companies hiring remotely, working from anywhere.",
    "Hobbies": "Hobbies and pastimes to pick up: crafts, sports, collecting, folding, and so on.",
    "Excel": "Excel and spreadsheets: formulas, shortcuts, Google Sheets, spreadsheet analysis.",
    "Projects": "Concrete things to build or make, with steps: DIY, maker or coding projects.",
}

CATEGORY_NAMES: tuple[str, ...] = tuple(CATEGORIES)
