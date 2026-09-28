# Call recordings

I recorded four calls to demonstrate the required paths through my deployed claims assistant. All caller details are synthetic. These are the complete recordings, converted to MP3 for easier playback.

| Scenario | Recording | What to listen for |
| --- | --- | --- |
| Happy path | [Listen (2:06)](recordings/01-happy-path.mp3) | I verify a test claim, ask for its status, and get the next steps for the required documents. |
| Authentication failure | [Listen (4:01)](recordings/02-authentication-failure.mp3) | I provide mismatched verification details. The agent does not disclose claim information and offers human help after the attempt limit. |
| Customer not found | [Listen (1:39)](recordings/03-customer-not-found.mp3) | I use a phone number that is not in the test data. The agent gives the same general verification response without revealing whether an account exists. |
| Representative escalation | [Listen (0:40)](recordings/04-representative-escalation.mp3) | I ask for a person without completing verification. A supervisor joins the call, and the agent hands over. |

The agent’s post-call interaction records are available in the [read-only Airtable base](https://airtable.com/appqWJg7QgjFwnH6r/shrVHa2edvKjYKSZq).

## Database schema

I’ve also included the [database schema diagram](supabase-schema.svg) so you can see the tables and relationships behind the demo.
