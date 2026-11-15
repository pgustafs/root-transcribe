# Notebooks: ideas for later

Ideas for the "Take notes" feature that are **not built yet**, kept here so
they are not lost. The research references are from general knowledge of the
learning literature, not a fresh review.

Already built (see README, "Notebooks: take notes while recording"): marks at
the press, a quick thought, the offer to keep a slide that changed, a
generated title, tidy transcript, text on the slide, key terms with a
glossary, a plain explanation for notes marked "Don't understand", wider
context from the full recording, and search.

## Build first: what the learner does after the video

These give the largest learning gains according to the research.

1. **Quiz me on my notebook.** An agent turns each note (image, transcript,
   own thoughts) into two or three questions; the learner answers before the
   note is shown. Testing yourself beats rereading by a wide margin (retrieval
   practice; Dunlosky et al. 2013 rate it among the most effective
   techniques). Can reuse the agent runner.
2. **Review schedule.** Each note comes back after 1, 3, 7 and 21 days; the
   learner marks "knew it" or "didn't". Spaced repetition is the other
   technique that review rates highly. A "Due today: 6 notes" list on the
   Notebooks page would do.
3. **Explain it back.** Before the transcript is shown, the note asks "What
   was the point here?" and the learner writes one sentence in *My thoughts*.
   An agent compares it with what was said and points out what is missing.
   Producing your own explanation is what makes notes stick (generation
   effect, self-explanation).

## Agents on a whole notebook

- **Study guide:** a summary built only from the notes, ordered by topic
  instead of by time.
- **Flashcard export:** cards for Anki, with the image on the front or back.
- **Gaps:** compare the notes with the full transcript and list the important
  parts that were not captured.
- **Ask my notebook:** chat that answers only from the notes and the
  recording, with a link to the note and timestamp it used.
- **Connections:** "this relates to your note from 3 November in another
  notebook", to tie new material to what the learner already knows.

## For video meetings

- **Decisions and actions:** an agent marks which notes contain a decision or
  a task, with owner and date.
- **Share a note:** send one note (image, quote, own comment) to a colleague
  or into the recording's comments.

## Smaller follow-ups to what is built

- A third mark, "Exam", was considered and left out; easy to add
  (`NOTE_TAGS` in `backend/app/models/study.py` and `frontend/src/api/study.ts`).
- Take a note **automatically** on every slide change (today the previous
  slide is only offered).
- Send a smaller copy of the image to the model: today the full PNG is sent,
  and images over 12 MB are not read.

## Things to be careful with

- **Summaries can replace thinking.** If the app writes everything, the
  learner does less of the mental work that creates memory. That is why
  quizzing and explain-back come before more automatic summaries.
- **Generated questions can be wrong.** They should quote the note they come
  from, and the learner should be able to edit or delete them.
- **Images go to a model.** Reading slides sends screenshots to the language
  model; restricted recordings need the same care as export.
