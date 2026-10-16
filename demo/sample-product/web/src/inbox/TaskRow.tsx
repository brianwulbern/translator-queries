export function TaskRow({task}) {
	return (
		<p className="task-row">
			{sub(I18n.get('x-sent-you-x-for-x'), [
				task.senderName,
				<a href={task.itemURL} key="item">
					{task.itemTitle}
				</a>,
				task.stepName,
			])}
		</p>
	);
}
